"""Human approval gate. Agents propose; named humans approve. Fails closed.

Rules (from policy["approval"]):
* LOW tier may be auto-approved with --auto-approve-low-risk, but NEVER when the write lands
  in a regulated table: those always need a named human.
* MEDIUM needs a named human with role >= standard; HIGH needs role elevated.
* The agent (or any planner identity) can never approve its own plan.
* No answer (EOF / non-interactive) means REJECTED.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable

ROLE_RANK = {"standard": 1, "elevated": 2}


@dataclass
class Approval:
    decision: str  # APPROVED | REJECTED
    approver: str
    approver_role: str
    channel: str  # interactive | auto-low-risk | policy | demo-simulated
    reason: str
    ts: str
    risk_tier: str
    required_role: str
    regulated_target: bool

    @property
    def approved(self) -> bool:
        return self.decision == "APPROVED"

    def to_dict(self) -> dict:
        return asdict(self)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def request_approval(
    *,
    plan_id: str,
    risk_tier: str,
    required_role: str,
    regulated: bool,
    policy: dict,
    summary: str,
    approver: str | None,
    approver_role: str = "standard",
    auto_approve_low_risk: bool = False,
    simulated_decision: str | None = None,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
) -> Approval:
    base = dict(risk_tier=risk_tier, required_role=required_role, regulated_target=regulated)
    tier_rule = policy["approval"][risk_tier]

    if auto_approve_low_risk and risk_tier == "LOW" and tier_rule.get("auto_approve_allowed") and not regulated:
        return Approval("APPROVED", "auto-policy", "policy", "auto-low-risk",
                        "LOW tier, non-regulated target, auto-approval enabled", _now(), **base)

    who = (approver or "").strip()
    if who.lower() in {a.lower() for a in policy.get("forbidden_approvers", [])}:
        return Approval("REJECTED", who, approver_role, "policy",
                        f"'{who}' may not approve (separation of duties: agents cannot approve their own plans)", _now(), **base)
    if not who:
        return Approval("REJECTED", "", approver_role, "policy",
                        "no named human approver supplied; failing closed", _now(), **base)
    if ROLE_RANK.get(approver_role, 0) < ROLE_RANK[required_role]:
        return Approval("REJECTED", who, approver_role, "policy",
                        f"{risk_tier} requires role '{required_role}', approver has '{approver_role}'", _now(), **base)

    if simulated_decision is not None:  # scripted demo reviewer; clearly labeled in the audit log
        d = "APPROVED" if simulated_decision.lower().startswith("y") else "REJECTED"
        return Approval(d, who, approver_role, "demo-simulated", "scripted demo reviewer", _now(), **base)

    print_fn(summary)
    try:
        ans = input_fn(f"Approve plan {plan_id} [{risk_tier}{', REGULATED' if regulated else ''}] as {who} ({approver_role})? [y/N]: ")
        reason = input_fn("Reason / ticket reference: ") if ans.strip().lower() in ("y", "yes") else (ans or "declined")
    except EOFError:
        return Approval("REJECTED", who, approver_role, "interactive", "no response (EOF); failing closed", _now(), **base)
    decision = "APPROVED" if ans.strip().lower() in ("y", "yes") else "REJECTED"
    return Approval(decision, who, approver_role, "interactive", reason.strip() or "-", _now(), **base)
