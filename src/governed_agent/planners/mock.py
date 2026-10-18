"""Deterministic rule-based planner so the demo runs offline. It deliberately includes a few
'careless agent' proposals (PII export, mass purge, DROP) to exercise the guardrails."""
from __future__ import annotations

import inspect
import json

from .. import __version__
from .base import PlannerOutput, build_prompt, sha256

TX = "bank.transactions"


def _currency():
    return dict(operation="FIX_DATA_QUALITY", target_table=TX,
                sql="SELECT * REPLACE (upper(trim(currency)) AS currency) FROM source",
                rationale="Currency codes arrive padded/lower-cased from one upstream feed; canonicalize to ISO-4217 upper case.",
                risk_tier="MEDIUM", expected_effect={"noncanonical_currency": 0})


def _category():
    return dict(operation="FIX_DATA_QUALITY", target_table=TX,
                sql="SELECT * REPLACE (coalesce(merchant_category, 'UNCATEGORIZED') AS merchant_category) FROM source",
                rationale="Null merchant_category breaks downstream spend reporting; use an explicit UNCATEGORIZED bucket instead of NULL.",
                risk_tier="MEDIUM", expected_effect={"null_merchant_category": 0})


def _sign():
    return dict(operation="FIX_DATA_QUALITY", target_table=TX,
                sql="SELECT * REPLACE (CASE WHEN txn_type = 'PURCHASE' AND amount < 0 THEN -amount ELSE amount END AS amount) FROM source",
                rationale="Purchases must be positive; negative purchase amounts are sign errors (refunds are typed REFUND).",
                risk_tier="MEDIUM", expected_effect={"negative_purchase_amount": 0})


def _dedup():
    return dict(operation="DEDUPLICATE", target_table=TX,
                sql="SELECT * FROM source QUALIFY row_number() OVER (PARTITION BY txn_id ORDER BY txn_ts, amount) = 1",
                rationale="Replayed messages created exact duplicate txn_ids; keep one row per txn_id.",
                risk_tier="MEDIUM", expected_effect={"duplicate_txn_id": 0})


def _band():
    return dict(operation="ADD_COLUMN", target_table=TX,
                sql=("SELECT *, CASE WHEN abs(amount) < 50 THEN 'SMALL' WHEN abs(amount) < 500 THEN 'MEDIUM' "
                     "ELSE 'LARGE' END AS amount_band FROM source"),
                new_columns=[{"name": "amount_band", "type": "string", "doc": "SMALL <50, MEDIUM <500, LARGE otherwise (abs amount)"}],
                rationale="Analysts repeatedly re-derive an amount band; materialize it once as an additive, nullable column.",
                risk_tier="LOW")


def _summary():
    return dict(operation="CREATE_DERIVED_TABLE", target_table=TX, output_table="analytics.daily_merchant_summary",
                sql=("SELECT CAST(txn_ts AS DATE) AS txn_date, coalesce(merchant_category, 'UNCATEGORIZED') AS merchant_category, "
                     "upper(trim(currency)) AS currency, count(*) AS txn_count, round(sum(amount), 2) AS total_amount, "
                     "count(DISTINCT sha256(account_id)) AS distinct_accounts "
                     "FROM source WHERE status = 'POSTED' GROUP BY ALL ORDER BY ALL"),
                rationale="Aggregated, PII-free daily spend by category for the analytics zone.",
                risk_tier="LOW")


def _marketing():
    return dict(operation="CREATE_DERIVED_TABLE", target_table=TX, output_table="analytics.marketing_contacts",
                sql="SELECT customer_name, customer_email, round(sum(amount), 2) AS spend_ytd FROM source GROUP BY ALL",
                rationale="Top spenders with contact details for a campaign.",
                risk_tier="LOW")


def _purge():
    return dict(operation="DELETE_ROWS", target_table=TX,
                sql="SELECT * FROM source WHERE NOT (txn_ts < TIMESTAMP '2026-04-01 00:00:00')",
                rationale="Reduce table size by removing Q1 history.",
                risk_tier="MEDIUM")


def _future():
    return dict(operation="DELETE_ROWS", target_table=TX,
                sql="SELECT * FROM source WHERE NOT (txn_ts > TIMESTAMP '2026-09-30 23:59:59')",
                rationale="Rows dated after the business date are clock-skew artifacts from one feed; remove them.",
                risk_tier="HIGH", expected_effect={"future_dated": 0})


def _drop():
    return dict(operation="DROP_TABLE", target_table=TX, sql="SELECT * FROM source WHERE false",
                rationale="Table is messy; start over.", risk_tier="HIGH")


RULES = [
    (("drop", "start over"), _drop),
    (("marketing", "campaign", "contact"), _marketing),
    (("purge", "q1", "old history"), _purge),
    (("future",), _future),
    (("currency",), _currency),
    (("category",), _category),
    (("negative", "sign"), _sign),
    (("duplicate", "dedup"), _dedup),
    (("band", "bucket", "add column"), _band),
    (("summary", "daily", "derived"), _summary),
]
PROFILE_RULES = [("noncanonical_currency", _currency), ("null_merchant_category", _category),
                 ("negative_purchase_amount", _sign), ("duplicate_txn_id", _dedup)]


class MockPlanner:
    name = "mock-planner"

    def propose(self, goal: str, context: dict) -> PlannerOutput:
        system, user = build_prompt(goal, context)
        g = goal.lower()
        plans = [fn() for keys, fn in RULES if any(k in g for k in keys)]
        if not plans:  # generic "fix data quality": one plan per detected issue
            prof = context.get("profile", {})
            plans = [fn() for metric, fn in PROFILE_RULES if prof.get(metric, 0) > 0]
        rules_src = inspect.getsource(inspect.getmodule(MockPlanner))
        raw = json.dumps({"plans": plans})
        prov = {
            "planner": self.name,
            "model": "rule-based-mock",
            "model_version": f"governed-iceberg-agent {__version__} / rules sha256:{sha256(rules_src)[:16]}",
            "endpoint": None,
            "temperature": None,
            "system_prompt_sha256": sha256(system),
            "user_prompt_sha256": sha256(user),
            "response_sha256": sha256(raw),
        }
        return PlannerOutput(plans, prov, system, user, raw)
