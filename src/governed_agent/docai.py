"""A small Document AI component: synthetic loan-application documents -> field extraction with a
confidence score per field -> confidence routing. Fields below the threshold go to a reviewer
queue; a document is only written to the regulated table once every low-confidence field has
been resolved by a named reviewer, and even then only through the approval gate.

SYNTHETIC DOCUMENTS ONLY.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import pyarrow as pa
from faker import Faker
import numpy as np

EXTRACTOR = "regex-extractor"
EXTRACTOR_VERSION = "1.0.0"
PURPOSES = ["HOME_IMPROVEMENT", "AUTO", "DEBT_CONSOLIDATION", "EDUCATION", "MEDICAL", "SMALL_BUSINESS"]
OCR_FIX = str.maketrans({"S": "5", "O": "0", "o": "0", "l": "1", "I": "1", "B": "8"})

LOAN_SCHEMA = pa.schema([
    pa.field("application_id", pa.string()),
    pa.field("applicant_name", pa.string()),   # PII
    pa.field("ssn", pa.string()),              # PII (synthetic)
    pa.field("annual_income", pa.float64()),
    pa.field("loan_amount", pa.float64()),
    pa.field("loan_purpose", pa.string()),
    pa.field("application_date", pa.date32()),
    pa.field("source_doc", pa.string()),       # provenance: which document
    pa.field("source_sha256", pa.string()),    # provenance: exact bytes
    pa.field("extractor", pa.string()),        # provenance: extractor name@version
    pa.field("min_confidence", pa.float64()),  # lowest model confidence across fields (pre-review)
    pa.field("reviewed_by", pa.string()),      # reviewer(s) who resolved low-confidence fields
    pa.field("reviewed_fields", pa.string()),
])


# ----------------------------------------------------------------------------- synthetic documents
def generate_documents(out_dir: Path, n: int = 8, seed: int = 7) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    fake, rng = Faker("en_US"), np.random.default_rng(seed)
    Faker.seed(seed)
    paths, truth = [], {}
    for i in range(n):
        income = int(rng.integers(35, 240)) * 1000
        amount = int(rng.integers(5, 80)) * 500
        f = {
            "Application ID": f"LA-2026-{100200 + i:06d}",
            "Applicant Name": fake.name(),
            "SSN": fake.ssn(),
            "Annual Income": f"${income:,}",
            "Requested Amount": f"${amount:,}",
            "Loan Purpose": PURPOSES[int(rng.integers(len(PURPOSES)))],
            "Application Date": f"2026-09-{int(rng.integers(1, 29)):02d}",
        }
        clean_date = f["Application Date"]
        # deterministic "scan quality" defects on some documents
        defect = i % 4
        if defect == 1:
            f["Annual Income"] = f["Annual Income"].replace("0", "O")  # OCR reads zeros as letter O
        elif defect == 2:
            f["Requested Amount"] = f["Requested Amount"][:2] + "?" + f["Requested Amount"][3:]
        elif defect == 3:
            y, m, d = f["Application Date"].split("-")
            f["Application Date"] = f"{m}/{d}/{y[2:]}"
        truth[f"loan_app_{i + 1:03d}.txt"] = {"annual_income": float(income), "loan_amount": float(amount),
                                               "application_date": clean_date}
        body = "SYNTHETIC LOAN APPLICATION (test document, not a real person)\n" + "\n".join(f"{k}: {v}" for k, v in f.items()) + "\n"
        p = out_dir / f"loan_app_{i + 1:03d}.txt"
        p.write_text(body)
        paths.append(p)
    # Answer key for the *simulated* reviewer in the demo/tests only (a human would read the scan).
    (out_dir / "_answer_key.json").write_text(json.dumps(truth, indent=1))
    return paths


# ----------------------------------------------------------------------------- extraction
@dataclass
class Field:
    value: object
    confidence: float
    raw: str | None
    note: str = ""


@dataclass
class Extraction:
    doc: str
    sha256: str
    fields: dict[str, Field] = field(default_factory=dict)

    @property
    def min_confidence(self) -> float:
        return min(f.confidence for f in self.fields.values())


def _label(text: str, label: str) -> str | None:
    m = re.search(rf"^{re.escape(label)}:\s*(.*)$", text, flags=re.M)
    return m.group(1).strip() if m else None


def _money(raw: str | None) -> Field:
    if raw is None:
        return Field(None, 0.0, None, "missing")
    if re.fullmatch(r"\$?\d{1,3}(,\d{3})*(\.\d{2})?", raw):
        return Field(float(raw.replace("$", "").replace(",", "")), 0.97, raw)
    fixed = raw.translate(OCR_FIX)
    if re.fullmatch(r"\$?\d{1,3}(,\d{3})*(\.\d{2})?", fixed):
        return Field(float(fixed.replace("$", "").replace(",", "")), 0.72, raw, "OCR character substitution corrected")
    return Field(None, 0.2, raw, "unreadable characters")


def _date(raw: str | None) -> Field:
    if raw is None:
        return Field(None, 0.0, None, "missing")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return Field(date.fromisoformat(raw), 0.98, raw)
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{2})", raw)
    if m:
        return Field(date(2000 + int(m.group(3)), int(m.group(1)), int(m.group(2))), 0.80, raw, "ambiguous MM/DD/YY format")
    return Field(None, 0.1, raw, "unparseable date")


def extract(path: Path) -> Extraction:
    data = Path(path).read_bytes()
    text = data.decode("utf-8", errors="replace")
    ex = Extraction(doc=Path(path).name, sha256=hashlib.sha256(data).hexdigest())
    aid = _label(text, "Application ID")
    ex.fields["application_id"] = Field(aid, 0.99 if aid and re.fullmatch(r"LA-\d{4}-\d{6}", aid) else 0.3, aid)
    name = _label(text, "Applicant Name")
    ex.fields["applicant_name"] = Field(name, 0.95 if name and re.fullmatch(r"[A-Za-z.' -]+", name) else 0.5, name)
    ssn = _label(text, "SSN")
    ex.fields["ssn"] = Field(ssn, 0.97 if ssn and re.fullmatch(r"\d{3}-\d{2}-\d{4}", ssn) else 0.3, ssn)
    ex.fields["annual_income"] = _money(_label(text, "Annual Income"))
    ex.fields["loan_amount"] = _money(_label(text, "Requested Amount"))
    purpose = _label(text, "Loan Purpose")
    ex.fields["loan_purpose"] = Field(purpose, 0.96 if purpose in PURPOSES else 0.4, purpose)
    ex.fields["application_date"] = _date(_label(text, "Application Date"))
    return ex


# ----------------------------------------------------------------------------- review queue
class ReviewQueue:
    """Append-only JSONL of QUEUED / RESOLVED events; current state is a fold over events."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _events(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]

    def _append(self, ev: dict) -> None:
        ev["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.path.open("a") as f:
            f.write(json.dumps(ev, default=str) + "\n")

    def state(self) -> dict[str, dict]:
        items: dict[str, dict] = {}
        for ev in self._events():
            if ev["event"] == "QUEUED":
                items.setdefault(ev["item_id"], {**ev, "status": "OPEN"})
            elif ev["event"] == "RESOLVED" and ev["item_id"] in items:
                items[ev["item_id"]].update(status="RESOLVED", value=ev["value"], reviewer=ev["reviewer"], resolved_ts=ev["ts"])
        return items

    def enqueue(self, ex: Extraction, field_name: str, f: Field, threshold: float) -> str | None:
        item_id = f"{ex.sha256[:8]}:{field_name}"
        if item_id in self.state():
            return None
        self._append({"event": "QUEUED", "item_id": item_id, "doc": ex.doc, "sha256": ex.sha256, "field": field_name,
                      "extracted_value": f.value, "raw": f.raw, "confidence": f.confidence, "threshold": threshold, "note": f.note})
        return item_id

    def resolve(self, item_id: str, value: str, reviewer: str) -> dict:
        st = self.state()
        if item_id not in st:
            raise KeyError(f"no review item {item_id}")
        if not reviewer:
            raise ValueError("a named reviewer is required")
        self._append({"event": "RESOLVED", "item_id": item_id, "value": value, "reviewer": reviewer})
        return self.state()[item_id]

    def open_items(self) -> list[dict]:
        return [i for i in self.state().values() if i["status"] == "OPEN"]


def _coerce(field_name: str, value):
    if value is None:
        return None
    if field_name in ("annual_income", "loan_amount"):
        return float(str(value).replace("$", "").replace(",", ""))
    if field_name == "application_date":
        return value if isinstance(value, date) else date.fromisoformat(str(value))
    return str(value)


def route(extractions: list[Extraction], queue: ReviewQueue, threshold: float) -> dict:
    """Return {'ready': [row dicts], 'held': [{doc, open_fields}], 'queued': [item_ids]}."""
    state = queue.state()
    ready, held, queued = [], [], []
    for ex in extractions:
        row, reviewers, reviewed, open_fields = {}, [], [], []
        for name, f in ex.fields.items():
            if f.confidence >= threshold:
                row[name] = _coerce(name, f.value)
                continue
            item_id = f"{ex.sha256[:8]}:{name}"
            item = state.get(item_id)
            if item is None:
                queue.enqueue(ex, name, f, threshold)
                queued.append(item_id)
                open_fields.append(name)
            elif item["status"] == "RESOLVED":
                row[name] = _coerce(name, item["value"])
                reviewers.append(item["reviewer"])
                reviewed.append(name)
            else:
                open_fields.append(name)
        if open_fields:
            held.append({"doc": ex.doc, "sha256": ex.sha256, "open_fields": open_fields})
            continue
        row.update(source_doc=ex.doc, source_sha256=ex.sha256, extractor=f"{EXTRACTOR}@{EXTRACTOR_VERSION}",
                   min_confidence=round(ex.min_confidence, 2),
                   reviewed_by=",".join(sorted(set(reviewers))) or None, reviewed_fields=",".join(reviewed) or None)
        ready.append(row)
    return {"ready": ready, "held": held, "queued": queued}
