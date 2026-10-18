"""Minimal OpenTelemetry-style tracer. Every tool call the agent makes becomes a span in
<home>/traces/traces.jsonl with trace_id / span_id / parent_span_id, timings, attributes and
status. Field names follow OTel conventions so the file can be mapped onto an OTLP exporter."""
from __future__ import annotations

import contextvars
import json
import secrets
import time
from contextlib import contextmanager
from pathlib import Path

_current: contextvars.ContextVar[dict | None] = contextvars.ContextVar("gia_span", default=None)


class Tracer:
    def __init__(self, path: Path, service_name: str = "governed-iceberg-agent"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.service_name = service_name

    @contextmanager
    def span(self, name: str, **attributes):
        parent = _current.get()
        span = {
            "trace_id": parent["trace_id"] if parent else secrets.token_hex(16),
            "span_id": secrets.token_hex(8),
            "parent_span_id": parent["span_id"] if parent else None,
            "name": name,
            "service.name": self.service_name,
            "start_time_unix_nano": time.time_ns(),
            "attributes": {k: v for k, v in attributes.items() if v is not None},
            "status": {"code": "OK"},
        }
        token = _current.set(span)
        try:
            yield span
        except BaseException as e:  # record and re-raise
            span["status"] = {"code": "ERROR", "message": f"{type(e).__name__}: {e}"[:500]}
            raise
        finally:
            _current.reset(token)
            span["end_time_unix_nano"] = time.time_ns()
            span["duration_ms"] = round((span["end_time_unix_nano"] - span["start_time_unix_nano"]) / 1e6, 3)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(span, default=str) + "\n")

    def read(self, trace_id: str | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        spans = [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]
        return [s for s in spans if trace_id is None or s["trace_id"] == trace_id]


def current_trace_id() -> str | None:
    s = _current.get()
    return s["trace_id"] if s else None


def set_attr(**attrs) -> None:
    s = _current.get()
    if s is not None:
        s["attributes"].update({k: v for k, v in attrs.items() if v is not None})
