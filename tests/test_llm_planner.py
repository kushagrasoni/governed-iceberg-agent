"""Exercises the OpenAI-compatible planner against a local fake endpoint (no network, no keys)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from governed_agent.planners.llm import LLMPlanner

PLANS = {"plans": [
    {"operation": "FIX_DATA_QUALITY", "target_table": "bank.transactions",
     "sql": "SELECT * REPLACE (upper(trim(currency)) AS currency) FROM source",
     "rationale": "canonicalize", "risk_tier": "LOW", "expected_effect": {"noncanonical_currency": 0}},
    {"operation": "FIX_DATA_QUALITY", "target_table": "bank.transactions",
     "sql": "DELETE FROM source WHERE amount < 0", "rationale": "prompt-injected", "risk_tier": "LOW"},
    {"operation": "FIX_DATA_QUALITY", "rationale": "missing fields"},
]}


class Fake(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Fake.seen.append((self.path, self.headers.get("Authorization"), body))
        out = {"id": "chatcmpl-test-1", "model": "fake-model-2026-01", "system_fingerprint": "fp_test",
               "choices": [{"message": {"role": "assistant", "content": json.dumps(PLANS)}}],
               "usage": {"prompt_tokens": 10, "completion_tokens": 20}}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_llm(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("GIA_LLM_BASE_URL", f"http://127.0.0.1:{srv.server_port}/v1")
    monkeypatch.setenv("GIA_LLM_API_KEY", "test-key")
    monkeypatch.setenv("GIA_LLM_MODEL", "fake-model")
    yield srv
    srv.shutdown()


def test_llm_plans_are_untrusted_and_governed(gov, fake_llm):
    pairs = gov.propose("fix currency", "llm")
    path, auth, body = Fake.seen[-1]
    assert path == "/v1/chat/completions" and auth == "Bearer test-key" and body["model"] == "fake-model"
    assert len(pairs) == 2  # the malformed third plan is rejected at validation
    assert any(r["event_type"] == "PLAN_REJECTED_INVALID" for r in gov.audit.records())
    good, bad = pairs
    prov = good[1]
    assert prov["model"] == "fake-model-2026-01" and prov["response_id"] == "chatcmpl-test-1"
    assert prov["model_version"] == "fp_test" and prov["endpoint"].startswith("127.0.0.1")
    out_bad = gov.run_plan(*bad, approver="r", simulated_decision="yes")
    assert out_bad.status == "BLOCKED_BY_POLICY" and "forbidden keywords" in out_bad.detail
    out_good = gov.run_plan(*good, approver="r", simulated_decision="yes")
    assert out_good.status == "EXECUTED" and out_good.risk_tier == "MEDIUM"  # declared LOW, raised by policy


def test_auto_planner_falls_back_to_mock_without_key(monkeypatch):
    from governed_agent.planners import MockPlanner, get_planner
    monkeypatch.delenv("GIA_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert isinstance(get_planner("auto"), MockPlanner)
    with pytest.raises(RuntimeError):
        LLMPlanner.from_env()
