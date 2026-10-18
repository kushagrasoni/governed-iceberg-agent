"""The governor: orchestrates propose -> policy -> dry run -> blast radius -> eval -> approve ->
publish, emitting a trace span for every tool call and an audit record for every decision."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pyarrow as pa

from . import __version__, docai
from .approval import request_approval
from .audit import AuditLog
from .config import Settings, get_catalog, load_policy
from .evals import evaluate
from .executor import Executor, RollbackRefused, StaleBaseError
from .lakehouse import AUDIT_TABLE, TRANSACTIONS, ensure_namespace, read, seed_transactions, table_exists
from .plan import Plan, PlanValidationError
from .planners import get_planner
from .planners.base import sha256
from .policy import blast_radius_check, static_check
from .profiler import profile
from .tracing import Tracer, current_trace_id, set_attr

LOANS = "bank.loan_applications"
AUDIT_SCHEMA = pa.schema([
    pa.field("seq", pa.int64()), pa.field("ts", pa.string()), pa.field("event_type", pa.string()),
    pa.field("plan_id", pa.string()), pa.field("actor", pa.string()), pa.field("trace_id", pa.string()),
    pa.field("prev_hash", pa.string()), pa.field("hash", pa.string()), pa.field("payload_json", pa.string()),
])


def _code_version() -> str:
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                             cwd=Path(__file__).parent, timeout=5).stdout.strip()
    except Exception:
        rev = ""
    return f"{__version__}+{rev}" if rev else __version__


@dataclass
class Outcome:
    plan_id: str
    operation: str
    target: str
    status: str  # EXECUTED | BLOCKED_BY_POLICY | EVAL_FAILED | REJECTED | ERROR
    risk_tier: str = ""
    detail: str = ""
    trace_id: str | None = None
    snapshots: dict = field(default_factory=dict)


class Governor:
    def __init__(self, settings: Settings, print_fn: Callable[[str], None] = print):
        self.settings = settings
        self.catalog = get_catalog(settings)
        self.policy = load_policy(settings)
        self.tracer = Tracer(settings.home / "traces" / "traces.jsonl")
        self.audit = AuditLog(settings.audit_path, mirror=self._mirror_audit)
        self.executor = Executor(self.catalog)
        self.print = print_fn
        self.queue = docai.ReviewQueue(settings.home / "docai" / "review_queue.jsonl")

    # ------------------------------------------------------------------ setup
    def _mirror_audit(self, rec: dict) -> None:
        if not table_exists(self.catalog, AUDIT_TABLE):
            return
        row = {k: rec.get(k) for k in AUDIT_SCHEMA.names if k != "payload_json"}
        row["payload_json"] = json.dumps(rec["payload"], default=str)
        self.catalog.load_table(AUDIT_TABLE).append(pa.Table.from_pylist([row], schema=AUDIT_SCHEMA))

    def _archive_evidence(self) -> None:
        """A forced re-init never deletes evidence: prior logs/queues move to archive/<utc-ts>/."""
        home = self.settings.home
        items = [p for p in (self.settings.audit_path, self.tracer.path, self.queue.path,
                             home / "provenance", self.settings.plans_dir, home / "docai" / "batches", home / "docs_in")
                 if p.exists() and (p.is_file() and p.stat().st_size > 0 or p.is_dir() and any(p.iterdir()))]
        if not items:
            return
        dest = home / "archive" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest.mkdir(parents=True, exist_ok=True)
        for p in items:
            p.rename(dest / str(p.relative_to(home)).replace("/", "__"))
        self.settings.ensure()

    def init(self, rows: int = 5000, seed: int = 42, force: bool = False) -> dict:
        with self.tracer.span("gia.init", rows=rows, seed=seed):
            ensure_namespace(self.catalog, "governance")
            ensure_namespace(self.catalog, "analytics")
            if force:
                self._archive_evidence()
                for t in (AUDIT_TABLE, LOANS, "analytics.daily_merchant_summary"):
                    if table_exists(self.catalog, t):
                        self.catalog.purge_table(t)
            if not table_exists(self.catalog, AUDIT_TABLE):
                self.catalog.create_table(AUDIT_TABLE, schema=AUDIT_SCHEMA, properties={"gia.append_only": "true"})
            manifest = seed_transactions(self.catalog, rows=rows, seed=seed, force=force)
            if not table_exists(self.catalog, LOANS):
                t = self.catalog.create_table(LOANS, schema=docai.LOAN_SCHEMA, properties={"gia.regulated": "true", "gia.synthetic": "true"})
                t.append(docai.LOAN_SCHEMA.empty_table(), snapshot_properties={"gia.event": "create-empty"})
            self.audit.append("LAKEHOUSE_INITIALIZED", None, "operator",
                              {"tables": [TRANSACTIONS, LOANS, AUDIT_TABLE], "seed_manifest": manifest,
                               "policy_version": self.policy["policy_version"], "synthetic": True},
                              current_trace_id())
            return manifest

    # ------------------------------------------------------------------ planning
    def context(self, table: str = TRANSACTIONS) -> dict:
        t = self.catalog.load_table(table)
        data = read(t)
        return {
            "target_table": table,
            "base_snapshot_id": t.current_snapshot().snapshot_id if t.current_snapshot() else None,
            "table_uuid": str(t.metadata.table_uuid),
            "schema": [{"name": f.name, "type": str(f.type)} for f in data.schema],
            "profile": profile(data) if "txn_id" in data.column_names else {"row_count": data.num_rows},
            "pii_columns": self.policy.get("pii_columns", {}).get(table, []),
            "allowed_operations": self.policy["allowed_operations"],
        }

    def propose(self, goal: str, planner_kind: str = "mock", table: str = TRANSACTIONS) -> list[tuple[Plan, dict]]:
        with self.tracer.span("agent.plan", goal=goal, planner_kind=planner_kind) as sp:
            with self.tracer.span("tool.profile_table", table=table):
                ctx = self.context(table)
            planner = get_planner(planner_kind)
            with self.tracer.span("llm.propose", planner=planner.name) as llm_span:
                out = planner.propose(goal, ctx)
                llm_span["attributes"].update({"gen_ai.request.model": out.provenance.get("model"),
                                               "gen_ai.response.id": out.provenance.get("response_id"),
                                               "plans_returned": len(out.plans)})
            results = []
            for raw in out.plans:
                try:
                    plan = Plan.from_dict(raw, planner=planner.name, goal=goal)
                except PlanValidationError as e:
                    self.audit.append("PLAN_REJECTED_INVALID", None, planner.name, {"error": str(e), "raw": raw}, current_trace_id())
                    self.print(f"  ! planner output rejected: {e}")
                    continue
                prov = self._record_provenance(plan, out, ctx)
                results.append((plan, prov))
            sp["attributes"]["plans_valid"] = len(results)
            return results

    def _record_provenance(self, plan: Plan, out, ctx: dict) -> dict:
        """NIST AI 600-1-style provenance: who/what generated the plan, from which inputs."""
        pdir = self.settings.home / "provenance"
        pdir.mkdir(exist_ok=True)
        prompt_file = pdir / f"{plan.plan_id}.prompt.json"
        prompt_file.write_text(json.dumps({"system": out.system_prompt, "user": out.user_prompt, "response": out.raw_response}, indent=1))
        inputs = [{"kind": "iceberg_table", "table": ctx["target_table"], "snapshot_id": ctx["base_snapshot_id"],
                   "table_uuid": ctx["table_uuid"]}]
        for name, path in (plan.inputs or {}).items():
            inputs.append({"kind": "staged_batch", "name": name, "path": str(path), "sha256": sha256(Path(path).read_text())})
        inputs.extend(out.provenance.get("documents", []))
        prov = {
            **{k: v for k, v in out.provenance.items() if k != "documents"},
            "prompt_record": str(prompt_file),
            "prompt_record_sha256": sha256(prompt_file.read_text()),
            "inputs": inputs,
            "policy_version": self.policy["policy_version"],
            "code_version": _code_version(),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        (self.settings.plans_dir / f"{plan.plan_id}.json").write_text(json.dumps({"plan": plan.to_dict(), "provenance": prov}, indent=2, default=str))
        return prov

    # ------------------------------------------------------------------ governed execution
    def run_plan(self, plan: Plan, provenance: dict, *, approver: str | None = None, approver_role: str = "standard",
                 auto_approve_low_risk: bool = False, simulated_decision: str | None = None,
                 input_fn: Callable[[str], str] = input) -> Outcome:
        with self.tracer.span("agent.run_plan", plan_id=plan.plan_id, operation=plan.operation, target=plan.target_table) as root:
            tid = root["trace_id"]
            target = plan.output_table or plan.target_table
            out = Outcome(plan.plan_id, plan.operation, target, "ERROR", plan.risk_tier, trace_id=tid)
            self.audit.append("PROPOSED", plan.plan_id, plan.planner, {"plan": plan.to_dict(), "provenance": provenance}, tid)

            with self.tracer.span("guardrail.static_policy"):
                pol = static_check(plan, self.policy, self.catalog)
                set_attr(decision=pol.decision, risk_tier=pol.risk_tier, failed=[c.name for c in pol.checks if not c.passed])
            self.audit.append("POLICY_EVALUATED", plan.plan_id, "policy-engine", pol.to_dict(), tid)
            out.risk_tier = pol.risk_tier
            if not pol.allowed:
                out.status, out.detail = "BLOCKED_BY_POLICY", "; ".join(c.detail for c in pol.checks if not c.passed)
                self.audit.append("BLOCKED", plan.plan_id, "policy-engine", {"stage": "static", "reason": out.detail}, tid)
                return out

            with self.tracer.span("tool.dry_run", mode="branch" if plan.operation in ("FIX_DATA_QUALITY", "DEDUPLICATE", "DELETE_ROWS", "APPEND_RECORDS") else "staging_table"):
                dr = self.executor.dry_run(plan)
                set_attr(staged_snapshot_id=dr.staged_snapshot_id, branch=dr.branch, staging_table=dr.staging_table, error=dr.error)
            self.audit.append("DRY_RUN_COMPLETED", plan.plan_id, "executor", dr.summary(), tid)
            if dr.error:
                self.executor.discard(dr)
                out.status, out.detail = "EVAL_FAILED", dr.error
                self.audit.append("DISCARDED", plan.plan_id, "executor", {"reason": dr.error}, tid)
                return out

            with self.tracer.span("eval.pre_execution"):
                ev = evaluate(plan, dr.pre, dr.post, dr.raw_result_schema, self.policy)
                set_attr(passed=ev.passed, failed=[c.name for c in ev.checks if not c.passed])
            with self.tracer.span("guardrail.blast_radius"):
                br = blast_radius_check(plan, pol, ev.diff, self.policy)
                set_attr(decision=br.decision, risk_tier=br.risk_tier)
            self.audit.append("EVAL_COMPLETED", plan.plan_id, "evaluator", ev.to_dict(), tid)
            self.audit.append("POLICY_EVALUATED", plan.plan_id, "policy-engine", br.to_dict(), tid)
            out.risk_tier = br.risk_tier
            if not br.allowed:
                self.executor.discard(dr)
                out.status = "BLOCKED_BY_POLICY"
                out.detail = "; ".join([c.detail for c in br.checks if not c.passed] +
                                       [f"{c.name}: {c.detail}" for c in ev.checks if not c.passed])
                self.audit.append("BLOCKED", plan.plan_id, "policy-engine", {"stage": "blast_radius", "reason": out.detail}, tid)
                return out
            if not ev.passed:
                self.executor.discard(dr)
                out.status, out.detail = "EVAL_FAILED", "; ".join(f"{c.name}: {c.detail}" for c in ev.checks if not c.passed)
                self.audit.append("DISCARDED", plan.plan_id, "executor", {"reason": out.detail}, tid)
                return out

            summary = self._approval_summary(plan, pol, br, ev, dr)
            with self.tracer.span("human.approval_gate", risk_tier=br.risk_tier, regulated=br.regulated):
                appr = request_approval(plan_id=plan.plan_id, risk_tier=br.risk_tier, required_role=br.required_role,
                                        regulated=br.regulated, policy=self.policy, summary=summary, approver=approver,
                                        approver_role=approver_role, auto_approve_low_risk=auto_approve_low_risk,
                                        simulated_decision=simulated_decision, input_fn=input_fn, print_fn=self.print)
                set_attr(decision=appr.decision, approver=appr.approver, channel=appr.channel)
            self.audit.append("APPROVAL_RECORDED", plan.plan_id, appr.approver or "nobody", appr.to_dict(), tid)
            if not appr.approved:
                self.executor.discard(dr)
                out.status, out.detail = "REJECTED", appr.reason
                self.audit.append("DISCARDED", plan.plan_id, "executor", {"reason": f"not approved: {appr.reason}"}, tid)
                return out

            with self.tracer.span("tool.iceberg_commit", target=target):
                try:
                    res = self.executor.publish(plan, dr, appr.to_dict())
                except StaleBaseError as e:
                    self.executor.discard(dr)
                    out.status, out.detail = "ERROR", str(e)
                    self.audit.append("DISCARDED", plan.plan_id, "executor", {"reason": str(e)}, tid)
                    return out
                set_attr(pre_snapshot_id=res.pre_snapshot_id, post_snapshot_id=res.post_snapshot_id)
            self.audit.append("EXECUTED", plan.plan_id, "executor", {
                "table": res.table, "pre_snapshot_id": res.pre_snapshot_id, "executed_snapshot_id": res.post_snapshot_id,
                "evaluated_snapshot_id": dr.staged_snapshot_id, "tag": f"approved-{plan.plan_id}",
                "rollback_pointer": res.rollback_pointer, "approver": appr.approver}, tid)
            out.status = "EXECUTED"
            out.detail = f"approved by {appr.approver} via {appr.channel}"
            out.snapshots = {"pre": res.pre_snapshot_id, "post": res.post_snapshot_id}
            return out

    def _approval_summary(self, plan, pol, br, ev, dr) -> str:
        lines = [
            "", "-" * 72,
            f"PLAN {plan.plan_id}  {plan.operation}  ->  {plan.output_table or plan.target_table}",
            f"  rationale : {plan.rationale}",
            f"  sql       : {plan.sql}",
            f"  risk tier : {br.risk_tier} (planner declared {plan.risk_tier}); regulated target: {br.regulated}",
            f"  dry run   : {dr.mode} {dr.branch or dr.staging_table} snapshot {dr.staged_snapshot_id}",
            f"  diff      : {ev.diff}",
            "  checks    : " + ", ".join(f"{c.name}={'ok' if c.passed else 'FAIL'}" for c in pol.checks + ev.checks + br.checks),
            "-" * 72,
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------ rollback
    def rollback(self, plan_id: str, approver: str, force: bool = False) -> dict:
        with self.tracer.span("agent.rollback", plan_id=plan_id) as sp:
            recs = self.audit.records(plan_id)
            executed = [r for r in recs if r["event_type"] == "EXECUTED"]
            if not executed:
                raise RollbackRefused(f"plan {plan_id} was never executed")
            if any(r["event_type"] == "ROLLED_BACK" for r in recs):
                raise RollbackRefused(f"plan {plan_id} was already rolled back")
            if not approver or approver.lower() in {a.lower() for a in self.policy.get("forbidden_approvers", [])}:
                raise RollbackRefused("rollback requires a named human operator")
            pointer = executed[-1]["payload"]["rollback_pointer"]
            with self.tracer.span("tool.iceberg_rollback", kind=pointer["kind"]):
                try:
                    res = self.executor.rollback(pointer, force=force)
                except RollbackRefused as e:
                    self.audit.append("ROLLBACK_REFUSED", plan_id, approver, {"reason": str(e), "pointer": pointer}, sp["trace_id"])
                    raise
            self.audit.append("ROLLED_BACK", plan_id, approver, {"pointer": pointer, "result": res, "forced": force}, sp["trace_id"])
            return res

    # ------------------------------------------------------------------ Document AI ingest
    def docai_ingest(self, docs_dir: Path) -> tuple[dict, list[tuple[Plan, dict]]]:
        threshold = float(self.policy["docai"]["confidence_threshold"])
        with self.tracer.span("docai.ingest", docs_dir=str(docs_dir), threshold=threshold):
            paths = sorted(Path(docs_dir).glob("*.txt"))
            exs = []
            for p in paths:
                with self.tracer.span("docai.extract", doc=p.name, extractor=f"{docai.EXTRACTOR}@{docai.EXTRACTOR_VERSION}") as s:
                    ex = docai.extract(p)
                    s["attributes"]["field_confidence"] = {k: f.confidence for k, f in ex.fields.items()}
                    s["attributes"]["source.sha256"] = ex.sha256
                exs.append(ex)
            loans = self.catalog.load_table(LOANS)
            already = set(read(loans).column("source_sha256").to_pylist())
            exs = [e for e in exs if e.sha256 not in already]
            with self.tracer.span("docai.route_by_confidence") as s:
                routed = docai.route(exs, self.queue, threshold)
                s["attributes"].update(ready=len(routed["ready"]), held=len(routed["held"]), queued=len(routed["queued"]))
            for item in routed["queued"]:
                st = self.queue.state()[item]
                self.audit.append("ROUTED_TO_REVIEW", None, "docai-router", st, current_trace_id())
            if not routed["ready"]:
                return routed, []
            bdir = self.settings.home / "docai" / "batches"
            bdir.mkdir(parents=True, exist_ok=True)
            batch = bdir / f"batch_{datetime.now(timezone.utc):%Y%m%dT%H%M%S%f}.json"
            batch.write_text(json.dumps(routed["ready"], default=str, indent=1))
            plan = Plan(operation="APPEND_RECORDS", target_table=LOANS,
                        sql="SELECT * FROM source UNION ALL BY NAME SELECT * FROM incoming",
                        rationale=f"Append {len(routed['ready'])} extracted loan applications whose fields all cleared the "
                                  f"{threshold:.2f} confidence threshold or were resolved by a named reviewer.",
                        risk_tier="MEDIUM", inputs={"incoming": str(batch)}, goal="ingest documents", planner="docai-router")
            ready_hashes = {r["source_sha256"] for r in routed["ready"]}
            documents = [{"kind": "document", "path": str(p), "sha256": e.sha256,
                          "field_confidence": {k: f.confidence for k, f in e.fields.items()}}
                         for p, e in zip(sorted(Path(docs_dir).glob("*.txt")), [docai.extract(p) for p in sorted(Path(docs_dir).glob("*.txt"))])
                         if e.sha256 in ready_hashes]

            class _Out:  # adapter so DocAI plans get the same provenance record as LLM plans
                provenance = {"planner": "docai-router", "model": docai.EXTRACTOR, "model_version": docai.EXTRACTOR_VERSION,
                              "confidence_threshold": threshold, "documents": documents}
                system_prompt, user_prompt, raw_response = "", json.dumps({"docs_dir": str(docs_dir)}), batch.read_text()
            ctx = self.context(LOANS)
            prov = self._record_provenance(plan, _Out, ctx)
            return routed, [(plan, prov)]
