"""The structured change plan an agent must emit. Free text is not executable; only plans are."""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

KNOWN_OPERATIONS = {
    "ADD_COLUMN", "CREATE_DERIVED_TABLE", "FIX_DATA_QUALITY", "DEDUPLICATE", "DELETE_ROWS", "APPEND_RECORDS",
    # Known-but-not-executable (the policy will refuse them):
    "DROP_TABLE", "DROP_COLUMN", "TRUNCATE", "EXPIRE_SNAPSHOTS", "REWRITE_SCHEMA_TYPE",
}
RISK_TIERS = ("LOW", "MEDIUM", "HIGH")

PLAN_JSON_SCHEMA = {
    "type": "object",
    "required": ["operation", "target_table", "sql", "rationale", "risk_tier"],
    "properties": {
        "operation": {"type": "string", "enum": sorted(KNOWN_OPERATIONS)},
        "target_table": {"type": "string", "description": "namespace.table being read (and changed, for in-place ops)"},
        "output_table": {"type": ["string", "null"], "description": "only for CREATE_DERIVED_TABLE"},
        "sql": {"type": "string", "description": "ONE DuckDB SELECT over relation `source` that returns the full post-change table"},
        "new_columns": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "type": {"type": "string"}, "doc": {"type": "string"}}}},
        "rationale": {"type": "string"},
        "risk_tier": {"type": "string", "enum": list(RISK_TIERS)},
        "expected_effect": {"type": "object", "description": "metric name -> expected value after the change, e.g. {\"duplicate_txn_id\": 0}"},
    },
}


class PlanValidationError(ValueError):
    pass


@dataclass
class Plan:
    operation: str
    target_table: str
    sql: str
    rationale: str
    risk_tier: str
    output_table: str | None = None
    new_columns: list[dict] = field(default_factory=list)
    expected_effect: dict = field(default_factory=dict)
    inputs: dict = field(default_factory=dict)  # extra named relations, e.g. {"incoming": "<path to staged JSON>"}
    goal: str = ""
    planner: str = "unknown"
    plan_id: str = field(default_factory=lambda: "p-" + uuid.uuid4().hex[:10])
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    @classmethod
    def from_dict(cls, d: dict, planner: str | None = None, goal: str | None = None) -> "Plan":
        """Validate untrusted planner output (LLM or file) into a Plan, or raise."""
        if not isinstance(d, dict):
            raise PlanValidationError("plan must be a JSON object")
        missing = [k for k in PLAN_JSON_SCHEMA["required"] if not d.get(k)]
        if missing:
            raise PlanValidationError(f"plan missing required fields: {missing}")
        op = str(d["operation"]).upper().strip()
        tier = str(d["risk_tier"]).upper().strip()
        if tier not in RISK_TIERS:
            raise PlanValidationError(f"risk_tier must be one of {RISK_TIERS}, got {tier!r}")
        for k in ("target_table", "output_table"):
            v = d.get(k)
            if v is not None and (not isinstance(v, str) or v.count(".") != 1):
                raise PlanValidationError(f"{k} must look like 'namespace.table', got {v!r}")
        new_cols = d.get("new_columns") or []
        if not isinstance(new_cols, list) or not all(isinstance(c, dict) and c.get("name") for c in new_cols):
            raise PlanValidationError("new_columns must be a list of {name, type} objects")
        kwargs = dict(
            operation=op,
            target_table=d["target_table"],
            sql=str(d["sql"]).strip(),
            rationale=str(d["rationale"]),
            risk_tier=tier,
            output_table=d.get("output_table"),
            new_columns=new_cols,
            expected_effect=dict(d.get("expected_effect") or {}),
            inputs=dict(d.get("inputs") or {}),
            goal=goal if goal is not None else d.get("goal", ""),
            planner=planner or d.get("planner", "unknown"),
        )
        if d.get("plan_id"):
            kwargs["plan_id"] = d["plan_id"]
        if d.get("created_at"):
            kwargs["created_at"] = d["created_at"]
        return cls(**kwargs)
