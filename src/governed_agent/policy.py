"""Guardrails. The policy is data (default_policy.json) owned by a risk function, and the
agent cannot lower the risk tier it is assigned: final tier = max(base, declared, escalations)."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from pyiceberg.catalog import Catalog

from .lakehouse import table_exists
from .plan import RISK_TIERS, Plan

SUPPORTED_NEW_COLUMN_TYPES = {"string", "double", "long", "int", "boolean", "timestamp", "date"}
FORBIDDEN_SQL_KEYWORDS = [
    "DROP", "DELETE", "TRUNCATE", "INSERT", "UPDATE", "MERGE", "ALTER", "GRANT", "REVOKE",
    "CREATE", "ATTACH", "DETACH", "COPY", "PRAGMA", "INSTALL", "LOAD", "EXPORT", "IMPORT",
    "CALL", "SET", "RESET", "VACUUM", "CHECKPOINT",
]


def tier_max(*tiers: str) -> str:
    return max(tiers, key=RISK_TIERS.index)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    escalate_to: str | None = None  # a passing check may still raise the tier


@dataclass
class PolicyResult:
    stage: str
    decision: str  # ALLOW | DENY
    risk_tier: str
    required_role: str
    checks: list[Check] = field(default_factory=list)
    regulated: bool = False  # writes to a regulated table always need a named human approver

    @property
    def allowed(self) -> bool:
        return self.decision == "ALLOW"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["failed"] = [c.name for c in self.checks if not c.passed]
        return d


def _strip_sql(sql: str) -> str:
    """Remove comments and string literals so keyword scans don't trip on data values."""
    s = re.sub(r"--[^\n]*", " ", sql)
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.S)
    s = re.sub(r"'(?:[^']|'')*'", "''", s)
    return s


def check_sql(sql: str) -> list[Check]:
    s = _strip_sql(sql).strip()
    checks = []
    body = s[:-1] if s.endswith(";") else s
    checks.append(Check("sql_single_statement", ";" not in body, "exactly one statement" if ";" not in body else "multiple statements found"))
    starts = re.match(r"^\s*(SELECT|WITH|FROM)\b", body, flags=re.I) is not None
    checks.append(Check("sql_is_select", starts, "read-only SELECT/WITH" if starts else "SQL must be a SELECT/WITH query"))
    hits = sorted({k for k in FORBIDDEN_SQL_KEYWORDS if re.search(rf"\b{k}\b", body, flags=re.I)})
    checks.append(Check("sql_no_ddl_dml", not hits, "no DDL/DML keywords" if not hits else f"forbidden keywords: {hits}"))
    return checks


def _pii_refs_unmasked(sql: str, pii_cols: list[str], masking_fns: list[str]) -> list[str]:
    """Return PII columns referenced in SQL outside of an approved masking function call."""
    s = _strip_sql(sql)
    bad = set()
    for col in pii_cols:
        for m in re.finditer(rf"\b{re.escape(col)}\b", s, flags=re.I):
            # walk outwards through enclosing parentheses, collecting function names
            depth, i, fns = 0, m.start() - 1, []
            while i >= 0:
                ch = s[i]
                if ch == ")":
                    depth += 1
                elif ch == "(":
                    if depth == 0:
                        fn = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*$", s[:i])
                        fns.append(fn.group(1).lower() if fn else "")
                    else:
                        depth -= 1
                i -= 1
            if not any(f in masking_fns for f in fns):
                bad.add(col)
    return sorted(bad)


def static_check(plan: Plan, policy: dict, catalog: Catalog) -> PolicyResult:
    checks: list[Check] = []
    op = plan.operation
    allowed_ops = set(policy["allowed_operations"])
    irreversible = set(policy.get("irreversible_operations", []))

    if op in irreversible:
        checks.append(Check("operation_allowlisted", False, f"{op} is irreversible on this platform; agents may only take reversible actions"))
    elif op not in allowed_ops:
        checks.append(Check("operation_allowlisted", False, f"{op} is not on the allowlist {sorted(allowed_ops)}"))
    else:
        checks.append(Check("operation_allowlisted", True, f"{op} is allowlisted"))

    tier = policy.get("base_risk_tier", {}).get(op, "HIGH")
    if op in policy.get("elevated_operations", []):
        checks.append(Check("elevated_operation", True, f"{op} requires an elevated approver", escalate_to="HIGH"))

    write_target = plan.output_table if op == "CREATE_DERIVED_TABLE" else plan.target_table
    ns = (write_target or "").split(".")[0]
    ok_ns = ns in policy.get("writable_namespaces", [])
    checks.append(Check("namespace_writable", ok_ns, f"namespace '{ns}' {'is' if ok_ns else 'is NOT'} writable by the agent"))

    exists = table_exists(catalog, plan.target_table)
    checks.append(Check("target_exists", exists, f"{plan.target_table} {'exists' if exists else 'does not exist'}"))
    if op == "CREATE_DERIVED_TABLE":
        if not plan.output_table:
            checks.append(Check("output_table_new", False, "CREATE_DERIVED_TABLE requires output_table"))
        else:
            clash = table_exists(catalog, plan.output_table)
            checks.append(Check("output_table_new", not clash, f"{plan.output_table} {'already exists (overwrite not allowed)' if clash else 'is a new table'}"))

    checks.extend(check_sql(plan.sql))

    pii_cols = policy.get("pii_columns", {}).get(plan.target_table, [])
    masking = [f.lower() for f in policy.get("pii_masking_functions", [])]
    if op == "CREATE_DERIVED_TABLE" and pii_cols:
        star = re.search(r"SELECT\s+(DISTINCT\s+)?\*", _strip_sql(plan.sql), flags=re.I) is not None
        unmasked = _pii_refs_unmasked(plan.sql, pii_cols, masking)
        ok = not star and not unmasked
        detail = "no raw PII leaves the source table" if ok else (
            "SELECT * would copy PII columns" if star else f"raw PII columns referenced: {unmasked} (allowed only inside {masking})")
        checks.append(Check("pii_derived_masking", ok, detail))
    elif pii_cols:
        touched = _pii_refs_unmasked(plan.sql, pii_cols, [])
        checks.append(Check("pii_in_place_untouched", True,
                            "SQL does not reference PII columns explicitly" if not touched else f"SQL references PII {touched}; escalated",
                            escalate_to="HIGH" if touched else None))

    if op == "ADD_COLUMN":
        names = [c["name"] for c in plan.new_columns]
        types_ok = all(str(c.get("type", "")).lower() in SUPPORTED_NEW_COLUMN_TYPES for c in plan.new_columns)
        checks.append(Check("new_columns_declared", bool(names) and types_ok,
                            f"new columns {names}" if names and types_ok else f"declare new_columns with types in {sorted(SUPPORTED_NEW_COLUMN_TYPES)}"))
        if exists:
            existing = set(catalog.load_table(plan.target_table).schema().column_names)
            clash = [n for n in names if n in existing]
            checks.append(Check("new_columns_are_new", not clash, "columns do not exist yet" if not clash else f"already exist: {clash}"))
        pii_like = [n for n in names if any(p in n.lower() for p in policy.get("pii_name_patterns", []))]
        checks.append(Check("new_column_not_pii", True,
                            "new columns do not look like PII" if not pii_like else f"new columns look like PII {pii_like}; escalated",
                            escalate_to="HIGH" if pii_like else None))

    declared = plan.risk_tier if plan.risk_tier in RISK_TIERS else "HIGH"
    tier = tier_max(tier, declared, *[c.escalate_to for c in checks if c.escalate_to])
    decision = "ALLOW" if all(c.passed for c in checks) else "DENY"
    role = policy["approval"][tier]["required_role"]
    regulated = write_target in policy.get("regulated_tables", []) or ns in policy.get("regulated_namespaces", [])
    return PolicyResult("static", decision, tier, role, checks, regulated)


def blast_radius_check(plan: Plan, prior: PolicyResult, diff: dict, policy: dict) -> PolicyResult:
    """Second policy gate, evaluated on the dry-run diff (rows actually changed)."""
    br = policy["blast_radius"]
    checks: list[Check] = []
    pre = max(1, diff["pre_rows"])
    # For appends, "blast radius" is what happens to rows that already existed; volume is capped separately.
    changed = diff["removed_or_modified"] if plan.operation == "APPEND_RECORDS" else diff["rows_changed"]
    changed_pct = 100.0 * changed / pre
    removed_pct = 100.0 * max(0, diff["pre_rows"] - diff["post_rows"]) / pre
    tier = prior.risk_tier

    if changed_pct > br["hard_limit_pct"]:
        checks.append(Check("blast_radius_hard_limit", False, f"{changed_pct:.2f}% of rows changed > hard limit {br['hard_limit_pct']}%"))
    else:
        esc = "HIGH" if changed_pct > br["escalate_to_high_pct"] else None
        checks.append(Check("blast_radius_hard_limit", True, f"{changed_pct:.2f}% of rows changed (limit {br['hard_limit_pct']}%)", escalate_to=esc))
    if removed_pct > br["max_rows_removed_pct"]:
        checks.append(Check("rows_removed_limit", False, f"{removed_pct:.2f}% of rows removed > {br['max_rows_removed_pct']}%"))
    else:
        checks.append(Check("rows_removed_limit", True, f"{removed_pct:.2f}% of rows removed (limit {br['max_rows_removed_pct']}%)"))

    if plan.operation == "APPEND_RECORDS":
        n, cap = diff["added_or_modified"], br.get("max_append_rows", 1000)
        checks.append(Check("append_volume_limit", n <= cap, f"{n} rows appended (limit {cap})"))

    tier = tier_max(tier, *[c.escalate_to for c in checks if c.escalate_to])
    decision = "ALLOW" if prior.allowed and all(c.passed for c in checks) else "DENY"
    return PolicyResult("blast_radius", decision, tier, policy["approval"][tier]["required_role"], checks, prior.regulated)
