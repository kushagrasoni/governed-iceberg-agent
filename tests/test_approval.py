from governed_agent.approval import request_approval
from conftest import plan_for


def req(gov, tier="MEDIUM", role="standard", regulated=True, **kw):
    return request_approval(plan_id="p-1", risk_tier=tier, required_role=gov.policy["approval"][tier]["required_role"],
                            regulated=regulated, policy=gov.policy, summary="", print_fn=lambda *_: None, **kw)


def test_auto_approve_only_low_and_non_regulated(gov):
    a = req(gov, tier="LOW", regulated=False, approver=None, auto_approve_low_risk=True)
    assert a.approved and a.channel == "auto-low-risk" and a.approver == "auto-policy"
    b = req(gov, tier="LOW", regulated=True, approver=None, auto_approve_low_risk=True)
    assert not b.approved and "no named human" in b.reason
    c = req(gov, tier="MEDIUM", regulated=False, approver=None, auto_approve_low_risk=True)
    assert not c.approved


def test_agent_cannot_approve_itself(gov):
    a = req(gov, approver="agent", simulated_decision="yes")
    assert not a.approved and "separation of duties" in a.reason


def test_high_tier_needs_elevated_role(gov):
    assert not req(gov, tier="HIGH", approver="alex", approver_role="standard", simulated_decision="yes").approved
    assert req(gov, tier="HIGH", approver="priya", approver_role="elevated", simulated_decision="yes").approved


def test_interactive_prompt_records_decision(gov):
    answers = iter(["y", "CHG-0001 reviewed diff"])
    a = req(gov, approver="alex", input_fn=lambda _: next(answers))
    assert a.approved and a.channel == "interactive" and a.reason == "CHG-0001 reviewed diff" and a.ts
    assert not req(gov, approver="alex", input_fn=lambda _: "n").approved


def test_eof_fails_closed(gov):
    def eof(_):
        raise EOFError
    a = req(gov, approver="alex", input_fn=eof)
    assert not a.approved and "EOF" in a.reason


def test_rejection_leaves_main_untouched_and_cleans_branch(gov):
    t = gov.catalog.load_table("bank.transactions")
    before = t.current_snapshot().snapshot_id
    plan, prov = plan_for(gov, "standardize currency")
    out = gov.run_plan(plan, prov, approver="alex", input_fn=lambda _: "n")
    assert out.status == "REJECTED"
    t.refresh()
    assert t.current_snapshot().snapshot_id == before
    assert not any(r.startswith("wap_") for r in t.refs())
    rec = [r for r in gov.audit.records(plan.plan_id) if r["event_type"] == "APPROVAL_RECORDED"][0]["payload"]
    assert rec["decision"] == "REJECTED" and rec["approver"] == "alex" and rec["ts"]
