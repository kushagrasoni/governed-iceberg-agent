"""Data-quality profile of a transactions-like table. Used by the planner (to find issues)
and by the evaluator (to assert that a change fixes what it claims and regresses nothing)."""
from __future__ import annotations

import pyarrow as pa

from .datagen import AS_OF
from .lakehouse import sandboxed_sql

ACCEPTED_CURRENCIES = ("USD", "EUR", "GBP", "INR", "CAD")

_METRICS = {
    "null_txn_id": ("txn_id", "count(*) FILTER (WHERE txn_id IS NULL)"),
    "duplicate_txn_id": ("txn_id", "count(*) - count(DISTINCT txn_id)"),
    "noncanonical_currency": (
        "currency",
        "count(*) FILTER (WHERE currency IS NOT NULL AND currency NOT IN ("
        + ", ".join(f"'{c}'" for c in ACCEPTED_CURRENCIES)
        + "))",
    ),
    "null_merchant_category": ("merchant_category", "count(*) FILTER (WHERE merchant_category IS NULL)"),
    "negative_purchase_amount": ("amount", "count(*) FILTER (WHERE txn_type = 'PURCHASE' AND amount < 0)"),
    "future_dated": ("txn_ts", f"count(*) FILTER (WHERE txn_ts > TIMESTAMP '{AS_OF:%Y-%m-%d %H:%M:%S}')"),
}


def profile(table: pa.Table) -> dict[str, int]:
    cols = set(table.column_names)
    exprs = ["count(*) AS row_count"]
    for name, (needs, expr) in _METRICS.items():
        if needs in cols and (name != "negative_purchase_amount" or "txn_type" in cols):
            exprs.append(f"{expr} AS {name}")
    res = sandboxed_sql(f"SELECT {', '.join(exprs)} FROM source", source=table).to_pylist()[0]
    return {k: int(v) for k, v in res.items()}
