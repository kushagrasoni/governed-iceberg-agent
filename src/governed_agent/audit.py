"""Append-only, hash-chained audit log (JSONL), optionally mirrored to an Iceberg table.

Each record carries prev_hash and hash = sha256(canonical JSON of the record without 'hash').
`verify()` recomputes the chain, so edits, deletions and reordering are detectable.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


def _canonical(rec: dict) -> str:
    return json.dumps(rec, sort_keys=True, separators=(",", ":"), default=str)


def _digest(rec: dict) -> str:
    body = {k: v for k, v in rec.items() if k != "hash"}
    return hashlib.sha256(_canonical(body).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: Path, mirror=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.mirror = mirror  # callable(record) -> None, e.g. Iceberg append; best-effort

    def _last(self) -> dict | None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return None
        with self.path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell() - 1
            while pos > 0:
                f.seek(pos - 1)
                if f.read(1) == b"\n":
                    break
                pos -= 1
            f.seek(max(pos, 0))
            return json.loads(f.readline())

    def append(self, event_type: str, plan_id: str | None, actor: str, payload: dict, trace_id: str | None = None) -> dict:
        last = self._last()
        rec = {
            "seq": (last["seq"] + 1) if last else 1,
            "event_id": uuid.uuid4().hex,
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event_type": event_type,
            "plan_id": plan_id,
            "actor": actor,
            "trace_id": trace_id,
            "payload": payload,
            "prev_hash": last["hash"] if last else GENESIS,
        }
        rec["hash"] = _digest(rec)
        # O_APPEND: the log is only ever extended, never rewritten.
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, (_canonical(rec) + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        if self.mirror is not None:
            try:
                self.mirror(rec)
            except Exception as e:  # the JSONL is the system of record; mirror failures are surfaced, not fatal
                rec["mirror_error"] = str(e)
        return rec

    def records(self, plan_id: str | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        out = [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]
        return [r for r in out if plan_id is None or r["plan_id"] == plan_id]

    def verify(self) -> tuple[bool, str]:
        prev, n = GENESIS, 0
        for i, rec in enumerate(self.records(), start=1):
            if rec.get("seq") != i:
                return False, f"sequence break at line {i} (seq={rec.get('seq')})"
            if rec.get("prev_hash") != prev:
                return False, f"chain break at seq {i}: prev_hash mismatch"
            if _digest(rec) != rec.get("hash"):
                return False, f"record seq {i} was modified (hash mismatch)"
            prev, n = rec["hash"], i
        return True, f"{n} records, chain intact, head={prev[:12]}"
