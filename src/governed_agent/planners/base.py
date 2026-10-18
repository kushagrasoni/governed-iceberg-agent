from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from ..plan import PLAN_JSON_SCHEMA

SYSTEM_PROMPT = """You are a data-engineering agent inside a bank's governed lakehouse.
You may only PROPOSE changes; a policy engine, an evaluator and a human approver decide.
Return JSON: {"plans": [<plan>, ...]} where each plan matches this JSON schema:
%s
Rules:
- `sql` is ONE DuckDB SELECT over the relation `source` (the current table) that returns the
  complete post-change table (or, for CREATE_DERIVED_TABLE, the complete new table).
- Never use DDL/DML. Deletions are expressed as SELECT ... WHERE NOT (<predicate>).
- Never output raw PII columns into derived tables; hash with sha256() if a key is needed.
- Prefer the smallest change that achieves the goal. State a risk_tier honestly; the platform
  may raise it but will never lower it.
- Put the data-quality metric you expect to fix in expected_effect, e.g. {"duplicate_txn_id": 0}.
"""


def build_prompt(goal: str, context: dict) -> tuple[str, str]:
    system = SYSTEM_PROMPT % json.dumps(PLAN_JSON_SCHEMA, indent=1)
    user = json.dumps({"goal": goal, **context}, indent=1, default=str)
    return system, user


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class PlannerOutput:
    plans: list[dict]
    provenance: dict = field(default_factory=dict)
    system_prompt: str = ""
    user_prompt: str = ""
    raw_response: str = ""
