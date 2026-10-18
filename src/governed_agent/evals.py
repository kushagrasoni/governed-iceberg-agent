"""Pre-execution evaluation of a staged change: schema compatibility, data-quality
assertions (absolute and no-regression), expected effect, PII integrity and a row-level diff.
Runs against what Iceberg actually stored on the branch / staging table, not the planner's claim."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import pyarrow as pa

from .lakehouse import sandboxed_sql
from .plan import Plan
from .profiler import profile

ALLOWED_PROMOTIONS = {("int32", "int64"), ("float", "double"), ("float32", "float64")}
SSN_RE = r"^\d{3}-\d{2}-\d{4}$"
EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$"


@dataclass
class EvalCheck:
    name: str
    passed: bool
    detail: str


@dataclass
class EvalResult:
    passed: bool
    checks: list[EvalCheck] = field(default_factory=list)
    diff: dict = field(default_factory=dict)
    profile_before: dict = field(default_factory=dict)
    profile_after: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["failed"] = [c.name for c in self.checks if not c.passed]
        return d


def row_diff(pre: pa.Table, post: pa.Table) -> dict:
    """Multiset diff on the pre-existing columns (NULL-safe, duplicates counted)."""
    cols = [c for c in pre.column_names if c in post.column_names]
    sel = ", ".join(f'"{c}"' for c in cols)
    q = f"""
      SELECT
        (SELECT count(*) FROM (SELECT {sel} FROM pre EXCEPT ALL SELECT {sel} FROM post)) AS removed_or_modified,
        (SELECT count(*) FROM (SELECT {sel} FROM post EXCEPT ALL SELECT {sel} FROM pre)) AS added_or_modified
    """
    r = sandboxed_sql(q, pre=pre, post=post).to_pylist()[0]
    return {
        "pre_rows": pre.num_rows,
        "post_rows": post.num_rows,
        "removed_or_modified": int(r["removed_or_modified"]),
        "added_or_modified": int(r["added_or_modified"]),
        "rows_changed": max(int(r["removed_or_modified"]), int(r["added_or_modified"])),
    }


def _norm(t: pa.DataType) -> str:
    """Arrow physical variants that map to the same Iceberg type are equivalent."""
    return {"large_string": "string", "large_binary": "binary"}.get(str(t), str(t))


def schema_compat(pre: pa.Schema, post: pa.Schema, declared_new: list[str]) -> EvalCheck:
    problems = []
    post_types = {f.name: f.type for f in post}
    for f in pre:
        if f.name not in post_types:
            problems.append(f"column removed: {f.name}")
        elif _norm(post_types[f.name]) != _norm(f.type) and (_norm(f.type), _norm(post_types[f.name])) not in ALLOWED_PROMOTIONS:
            problems.append(f"type change {f.name}: {f.type} -> {post_types[f.name]}")
    extra = [n for n in post_types if n not in pre.names]
    undeclared = [n for n in extra if n not in declared_new]
    missing_new = [n for n in declared_new if n not in post_types]
    if undeclared:
        problems.append(f"undeclared new columns: {undeclared}")
    if missing_new:
        problems.append(f"declared columns not produced: {missing_new}")
    ok = not problems
    return EvalCheck("schema_compatible", ok, "backward compatible (additive only)" if ok else "; ".join(problems))


def _pii_value_hits(t: pa.Table) -> dict[str, int]:
    hits = {}
    for f in t.schema:
        if pa.types.is_string(f.type) or pa.types.is_large_string(f.type):
            q = f"""SELECT count(*) FILTER (WHERE regexp_matches("{f.name}", '{SSN_RE}') OR regexp_matches("{f.name}", '{EMAIL_RE}')) AS n FROM t"""
            n = int(sandboxed_sql(q, t=t).to_pylist()[0]["n"])
            if n:
                hits[f.name] = n
    return hits


def evaluate(plan: Plan, pre: pa.Table, post: pa.Table, raw_result_schema: pa.Schema, policy: dict) -> EvalResult:
    checks: list[EvalCheck] = []
    tbl_rules = policy.get("dq_assertions", {}).get(plan.target_table, {})
    pii_cols = policy.get("pii_columns", {}).get(plan.target_table, [])
    patterns = policy.get("pii_name_patterns", [])

    if plan.operation == "CREATE_DERIVED_TABLE":
        checks.append(EvalCheck("output_nonempty", post.num_rows > 0, f"{post.num_rows} rows"))
        bad_names = [n for n in post.column_names
                     if n in pii_cols or (any(p in n.lower() for p in patterns) and not n.lower().endswith(("_sha256", "_hash")))]
        checks.append(EvalCheck("no_pii_column_names", not bad_names, "no PII-like column names" if not bad_names else f"PII-like columns: {bad_names}"))
        hits = _pii_value_hits(post)
        checks.append(EvalCheck("no_pii_values", not hits, "no SSN/email-shaped values in output" if not hits else f"PII-shaped values found: {hits}"))
        # the source is untouched by construction; record a zero-change diff for the blast-radius gate
        diff = {"pre_rows": pre.num_rows, "post_rows": pre.num_rows, "removed_or_modified": 0, "added_or_modified": 0,
                "rows_changed": 0, "output_rows": post.num_rows}
        return EvalResult(all(c.passed for c in checks), checks, diff)

    declared_new = [c["name"] for c in plan.new_columns]
    checks.append(schema_compat(pre.schema, raw_result_schema, declared_new))
    diff = row_diff(pre, post)

    if plan.operation == "ADD_COLUMN":
        ok = diff["rows_changed"] == 0 and diff["pre_rows"] == diff["post_rows"]
        checks.append(EvalCheck("existing_columns_unchanged", ok, "existing values identical" if ok else f"existing data changed: {diff}"))
    if plan.operation in ("FIX_DATA_QUALITY", "ADD_COLUMN"):
        ok = diff["pre_rows"] == diff["post_rows"]
        checks.append(EvalCheck("row_count_preserved", ok, f"{diff['pre_rows']} -> {diff['post_rows']}"))
    if plan.operation in ("DEDUPLICATE", "DELETE_ROWS"):
        ok = diff["post_rows"] <= diff["pre_rows"] and diff["added_or_modified"] == 0
        checks.append(EvalCheck("only_rows_removed", ok, f"{diff['pre_rows']} -> {diff['post_rows']}, modified={diff['added_or_modified']}"))
    if plan.operation == "APPEND_RECORDS":
        ok = diff["removed_or_modified"] == 0
        checks.append(EvalCheck("existing_rows_unchanged", ok, f"appended {diff['added_or_modified']} rows, modified/removed {diff['removed_or_modified']}"))

    for col in tbl_rules.get("not_null", []):
        if col in post.column_names:
            n = post.column(col).null_count
            checks.append(EvalCheck(f"not_null:{col}", n == 0, f"{n} nulls"))
    if plan.operation == "APPEND_RECORDS":
        for col in policy.get("docai", {}).get("required_provenance_columns", []):
            n = post.column(col).null_count if col in post.column_names else -1
            checks.append(EvalCheck(f"provenance_present:{col}", n == 0, f"{n} rows missing provenance" if n else "present on every row"))

    before, after = {}, {}
    if {"txn_id", "amount"} <= set(pre.column_names):
        before, after = profile(pre), profile(post)
        regress = [m for m in tbl_rules.get("no_regression_metrics", []) if after.get(m, 0) > before.get(m, 0)]
        checks.append(EvalCheck("dq_no_regression", not regress, "no DQ metric got worse" if not regress else f"regressed: {regress}"))
        for metric, target in (plan.expected_effect or {}).items():
            if metric in after:
                ok = after[metric] <= int(target) and (after[metric] < before[metric] or before[metric] <= int(target))
                checks.append(EvalCheck(f"expected_effect:{metric}", ok, f"{before[metric]} -> {after[metric]} (target <= {target})"))
            else:
                checks.append(EvalCheck(f"expected_effect:{metric}", False, f"unknown metric {metric}"))

    present_pii = [c for c in pii_cols if c in pre.column_names and c in post.column_names]
    if present_pii:
        sel = ", ".join(f'"{c}"' for c in present_pii)
        n = int(sandboxed_sql(f"SELECT count(*) AS n FROM (SELECT {sel} FROM post EXCEPT ALL SELECT {sel} FROM pre)", pre=pre, post=post).to_pylist()[0]["n"])
        if plan.operation == "APPEND_RECORDS":
            checks.append(EvalCheck("pii_values_unchanged", True, f"{n} PII tuples arrive with new, provenance-tagged rows"))
        else:
            checks.append(EvalCheck("pii_values_unchanged", n == 0, "no PII value altered or introduced" if n == 0 else f"{n} PII tuples altered/introduced"))

    return EvalResult(all(c.passed for c in checks), checks, diff, before, after)
