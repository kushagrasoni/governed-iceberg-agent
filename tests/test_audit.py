import json

from governed_agent.audit import AuditLog
from governed_agent.lakehouse import read
from conftest import approve, plan_for


def test_hash_chain_detects_tampering(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    for i in range(5):
        log.append("E", f"p{i}", "t", {"i": i})
    assert log.verify()[0]
    lines = log.path.read_text().splitlines()
    rec = json.loads(lines[2]); rec["payload"]["i"] = 99
    lines[2] = json.dumps(rec)
    log.path.write_text("\n".join(lines) + "\n")
    ok, msg = log.verify()
    assert not ok and "modified" in msg


def test_hash_chain_detects_deletion(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    for i in range(4):
        log.append("E", None, "t", {"i": i})
    lines = log.path.read_text().splitlines()
    del lines[1]
    log.path.write_text("\n".join(lines) + "\n")
    assert not log.verify()[0]


def test_executed_plan_has_full_evidence_trail(gov):
    plan, prov = plan_for(gov, "standardize currency")
    out = approve(gov, (plan, prov))
    assert out.status == "EXECUTED"
    events = [r["event_type"] for r in gov.audit.records(plan.plan_id)]
    assert events == ["PROPOSED", "POLICY_EVALUATED", "DRY_RUN_COMPLETED", "EVAL_COMPLETED",
                      "POLICY_EVALUATED", "APPROVAL_RECORDED", "EXECUTED"]
    ex = gov.audit.records(plan.plan_id)[-1]["payload"]
    t = gov.catalog.load_table("bank.transactions")
    assert ex["executed_snapshot_id"] == t.current_snapshot().snapshot_id == ex["evaluated_snapshot_id"]
    assert ex["rollback_pointer"]["restore_snapshot_id"] == ex["pre_snapshot_id"]
    assert f"approved-{plan.plan_id}" in t.refs()
    # Iceberg snapshot summary links back to the plan
    assert t.current_snapshot().summary.additional_properties["gia.plan_id"] == plan.plan_id
    # audit mirrored into an Iceberg table
    assert read(gov.catalog.load_table("governance.agent_audit")).num_rows == len(gov.audit.records())
    assert gov.audit.verify()[0]


def test_provenance_records_model_prompt_and_inputs(gov):
    plan, prov = plan_for(gov, "standardize currency")
    assert prov["model"] == "rule-based-mock" and prov["model_version"]
    assert len(prov["user_prompt_sha256"]) == 64 and len(prov["system_prompt_sha256"]) == 64
    snap = gov.catalog.load_table("bank.transactions").current_snapshot().snapshot_id
    assert prov["inputs"][0]["snapshot_id"] == snap
    stored = json.loads(open(prov["prompt_record"]).read())
    assert "standardize currency" in stored["user"]


def test_every_step_is_traced(gov):
    out = approve(gov, plan_for(gov, "standardize currency"))
    spans = gov.tracer.read(out.trace_id)
    names = {s["name"] for s in spans}
    assert {"agent.run_plan", "guardrail.static_policy", "tool.dry_run", "eval.pre_execution",
            "guardrail.blast_radius", "human.approval_gate", "tool.iceberg_commit"} <= names
    root = [s for s in spans if s["parent_span_id"] is None][0]
    assert all(s["parent_span_id"] == root["span_id"] for s in spans if s is not root)
