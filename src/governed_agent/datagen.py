"""Synthetic bank-like card/ACH transactions with deliberately seeded data-quality defects.

SYNTHETIC DATA ONLY. Names, emails and SSN-formatted strings come from Faker and do
not correspond to real people.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pyarrow as pa
from faker import Faker

AS_OF = datetime(2026, 9, 30, 23, 59, 59)  # "business date" of the synthetic snapshot
START = datetime(2026, 1, 1)

SCHEMA = pa.schema(
    [
        pa.field("txn_id", pa.string()),
        pa.field("account_id", pa.string()),
        pa.field("customer_name", pa.string()),   # PII
        pa.field("customer_email", pa.string()),  # PII
        pa.field("ssn", pa.string()),             # PII (synthetic, Faker-generated)
        pa.field("txn_ts", pa.timestamp("us")),
        pa.field("amount", pa.float64()),
        pa.field("currency", pa.string()),
        pa.field("merchant", pa.string()),
        pa.field("merchant_category", pa.string()),
        pa.field("channel", pa.string()),
        pa.field("txn_type", pa.string()),
        pa.field("status", pa.string()),
    ]
)

CATEGORIES = ["GROCERY", "FUEL", "TRAVEL", "DINING", "UTILITIES", "RETAIL", "HEALTH", "ENTERTAINMENT"]
CHANNELS = ["CARD_PRESENT", "ECOM", "ACH", "WIRE", "MOBILE"]
CURRENCIES = ["USD"] * 85 + ["EUR"] * 6 + ["GBP"] * 4 + ["CAD"] * 3 + ["INR"] * 2


def generate(rows: int = 5000, seed: int = 42) -> tuple[pa.Table, dict]:
    """Return (table, manifest). The manifest records how many defects of each kind were seeded."""
    rng = np.random.default_rng(seed)
    fake = Faker("en_US")
    Faker.seed(seed)

    n_customers = max(50, rows // 12)
    customers = [
        (f"ACC{100000 + i}", fake.name(), fake.free_email(), fake.ssn()) for i in range(n_customers)
    ]
    merchants = [fake.company() for _ in range(120)]
    span = int((AS_OF - START).total_seconds())

    base = rows - int(rows * 0.015)  # leave room for exact duplicate rows
    recs: list[dict] = []
    for i in range(base):
        acc, name, email, ssn = customers[int(rng.integers(n_customers))]
        ttype = rng.choice(["PURCHASE", "REFUND", "TRANSFER"], p=[0.82, 0.06, 0.12])
        amt = float(np.round(rng.lognormal(3.6, 1.1), 2))
        if ttype == "REFUND":
            amt = -amt
        recs.append(
            dict(
                txn_id=f"T{seed:02d}{i:08d}",
                account_id=acc,
                customer_name=name,
                customer_email=email,
                ssn=ssn,
                txn_ts=START + timedelta(seconds=int(rng.integers(span))),
                amount=amt,
                currency=str(rng.choice(CURRENCIES)),
                merchant=merchants[int(rng.integers(len(merchants)))],
                merchant_category=str(rng.choice(CATEGORIES)),
                channel=str(rng.choice(CHANNELS)),
                txn_type=str(ttype),
                status=str(rng.choice(["POSTED", "PENDING", "REVERSED"], p=[0.93, 0.05, 0.02])),
            )
        )

    manifest = {"rows_clean_base": base}

    def pick(frac: float) -> np.ndarray:
        k = max(1, int(base * frac))
        return rng.choice(base, size=k, replace=False)

    # 1) Non-canonical currency codes (lowercase / padded / mixed-case)
    idx = pick(0.04)
    for j, r in enumerate(idx):
        recs[r]["currency"] = [" usd", "usd", "Usd ", "eur", " GBP "][j % 5]
    manifest["noncanonical_currency"] = len(idx)

    # 2) Missing merchant category
    idx = pick(0.03)
    for r in idx:
        recs[r]["merchant_category"] = None
    manifest["null_merchant_category"] = len(idx)

    # 3) Sign errors: purchases recorded as negative amounts
    purchases = [i for i, r in enumerate(recs) if r["txn_type"] == "PURCHASE" and r["amount"] > 0]
    idx = rng.choice(purchases, size=max(1, int(base * 0.01)), replace=False)
    for r in idx:
        recs[r]["amount"] = -recs[r]["amount"]
    manifest["negative_purchase_amount"] = len(idx)

    # 4) Future-dated rows (clock skew in an upstream feed)
    idx = pick(0.005)
    for r in idx:
        recs[r]["txn_ts"] = AS_OF + timedelta(days=int(rng.integers(1, 40)))
    manifest["future_dated"] = len(idx)

    # 5) Exact duplicate rows (replayed messages)
    dup_src = rng.choice(base, size=rows - base, replace=False)
    recs.extend(dict(recs[r]) for r in dup_src)
    manifest["duplicate_rows"] = len(dup_src)

    order = rng.permutation(len(recs))
    recs = [recs[i] for i in order]
    table = pa.Table.from_pylist(recs, schema=SCHEMA)
    manifest["rows_total"] = table.num_rows
    return table, manifest
