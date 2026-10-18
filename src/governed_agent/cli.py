"""`gia` command line. Run `gia demo` for the end-to-end walkthrough (offline, synthetic data)."""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from collections import Counter
from pathlib import Path

from . import docai
from .config import get_settings
from .executor import RollbackRefused
from .lakehouse import TRANSACTIONS, read
from .pipeline import LOANS, Governor
from .plan import Plan
from .profiler import profile


def _gov(args) -> Governor:
    return Governor(get_settings(args.home))


def _print_outcome(o) -> None:
    mark = {"EXECUTED": "EXECUTED", "BLOCKED_BY_POLICY": "BLOCKED ", "EVAL_FAILED": "EVAL-FAIL", "REJECTED": "REJECTED", "ERROR": "ERROR   "}[o.status]
    print(f"  [{mark}] {o.plan_id} {o.operation:<20} tier={o.risk_tier:<6} -> {o.target}")
    print(f"             {o.detail}")
    if o.snapshots:
        print(f"             snapshot {o.snapshots.get('pre')} -> {o.snapshots.get('post')}   trace={o.trace_id}")


def cmd_init(args):
    m = _gov(args).init(rows=args.rows, seed=args.seed, force=args.force)
    print(json.dumps(m, indent=2))


def cmd_profile(args):
    g = _gov(args)
    print(json.dumps(profile(read(g.catalog.load_table(TRANSACTIONS))), indent=2))


def cmd_propose(args):
    g = _gov(args)
    for plan, prov in g.propose(args.goal, args.planner):
        print(plan.to_json())
        print(f"# provenance: {g.settings.plans_dir / (plan.plan_id + '.json')}")


def _run_plans(g: Governor, pairs, args) -> list:
    outs = []
    for plan, prov in pairs:
        o = g.run_plan(plan, prov, approver=args.approver, approver_role=args.approver_role,
                       auto_approve_low_risk=args.auto_approve_low_risk,
                       simulated_decision=getattr(args, "simulated_decision", None))
        _print_outcome(o)
        outs.append(o)
    return outs


def cmd_run(args):
    g = _gov(args)
    if args.plan_file:
        d = json.loads(Path(args.plan_file).read_text())
        plan = Plan.from_dict(d.get("plan", d), planner="file")
        pairs = [(plan, d.get("provenance", {"planner": "file", "source": args.plan_file}))]
    else:
        pairs = g.propose(args.goal, args.planner)
    outs = _run_plans(g, pairs, args)
    sys.exit(0 if all(o.status == "EXECUTED" for o in outs) else 3)


def cmd_rollback(args):
    g = _gov(args)
    try:
        res = g.rollback(args.plan_id, approver=args.approver, force=args.force)
    except RollbackRefused as e:
        print(f"ROLLBACK REFUSED: {e}")
        sys.exit(4)
    print(json.dumps(res, indent=2))


def cmd_audit(args):
    g = _gov(args)
    if args.verify:
        ok, msg = g.audit.verify()
        print(("OK   " if ok else "FAIL ") + msg)
        sys.exit(0 if ok else 5)
    for r in g.audit.records(args.plan):
        p = r["payload"]
        extra = ""
        if r["event_type"] == "POLICY_EVALUATED":
            extra = f"{p['stage']}: {p['decision']} tier={p['risk_tier']} failed={p['failed']}"
        elif r["event_type"] == "APPROVAL_RECORDED":
            extra = f"{p['decision']} by {p['approver'] or '-'} ({p['channel']}) at {p['ts']}"
        elif r["event_type"] == "EXECUTED":
            extra = f"{p['table']} {p['pre_snapshot_id']} -> {p['executed_snapshot_id']} rollback={p['rollback_pointer']['kind']}"
        elif r["event_type"] in ("BLOCKED", "DISCARDED", "ROLLBACK_REFUSED"):
            extra = p.get("reason", "")
        elif r["event_type"] == "PROPOSED":
            extra = f"{p['plan']['operation']} by {p['provenance'].get('model')} prompt={p['provenance'].get('user_prompt_sha256', '')[:12]}"
        elif r["event_type"] == "EVAL_COMPLETED":
            extra = f"passed={p['passed']} failed={p['failed']} diff={p['diff']}"
        print(f"{r['seq']:>4} {r['ts']} {r['event_type']:<20} {r['plan_id'] or '-':<14} {r['actor']:<16} {extra}"[:240])


def cmd_history(args):
    g = _gov(args)
    t = g.catalog.load_table(args.table)
    cur = t.current_snapshot().snapshot_id if t.current_snapshot() else None
    refs: dict = {}
    for name, ref in t.refs().items():
        refs.setdefault(ref.snapshot_id, []).append(name)
    for s in t.snapshots():
        summ = s.summary.additional_properties if s.summary else {}
        mark = "*" if s.snapshot_id == cur else " "
        print(f"{mark} {s.snapshot_id:<20} parent={s.parent_snapshot_id or '-':<20} {s.summary.operation.value if s.summary else '':<9} "
              f"plan={summ.get('gia.plan_id', '-'):<14} stage={summ.get('gia.stage', summ.get('gia.event', '-')):<8} refs={','.join(sorted(refs.get(s.snapshot_id, [])))}")


def cmd_trace(args):
    g = _gov(args)
    spans = g.tracer.read(args.trace_id)
    if not args.trace_id:
        print(json.dumps(Counter(s["name"] for s in spans), indent=2))
        return
    by_parent: dict = {}
    for s in spans:
        by_parent.setdefault(s["parent_span_id"], []).append(s)

    def walk(pid, depth):
        for s in sorted(by_parent.get(pid, []), key=lambda x: x["start_time_unix_nano"]):
            attrs = {k: v for k, v in s["attributes"].items() if k in ("decision", "risk_tier", "approver", "post_snapshot_id", "staged_snapshot_id", "passed", "failed")}
            print(f"{'  ' * depth}{s['name']:<28} {s['duration_ms']:>9.1f} ms  {s['status']['code']}  {attrs if attrs else ''}")
            walk(s["span_id"], depth + 1)
    walk(None, 0)


def cmd_docai(args):
    g = _gov(args)
    if args.action == "generate":
        paths = docai.generate_documents(Path(args.docs), n=args.n, seed=args.seed)
        print(f"generated {len(paths)} synthetic documents in {args.docs}")
        return
    routed, pairs = g.docai_ingest(Path(args.docs))
    print(f"  extracted: ready={len(routed['ready'])} held_for_review={len(routed['held'])} new_review_items={len(routed['queued'])}")
    for h in routed["held"]:
        print(f"    held {h['doc']}: low-confidence fields {h['open_fields']}")
    _run_plans(g, pairs, args)


def cmd_review(args):
    g = _gov(args)
    if args.action == "list":
        for i in g.queue.state().values():
            print(f"  {i['item_id']:<28} {i['status']:<9} {i['doc']} {i['field']:<17} raw={i['raw']!r:<14} conf={i['confidence']:.2f} {i.get('note', '')}"
                  + (f" -> {i['value']!r} by {i['reviewer']}" if i["status"] == "RESOLVED" else ""))
        return
    item = g.queue.resolve(args.item_id, args.value, args.reviewer)
    g.audit.append("REVIEW_RESOLVED", None, args.reviewer, item, None)
    print(f"  resolved {args.item_id} = {args.value!r} by {args.reviewer}")


def _add_approval_flags(p):
    p.add_argument("--approver", help="named human approver recorded in the audit log")
    p.add_argument("--approver-role", default="standard", choices=["standard", "elevated"])
    p.add_argument("--auto-approve-low-risk", action="store_true",
                   help="auto-approve LOW tier plans that do not write to a regulated table")
    p.add_argument("--planner", default="auto", choices=["auto", "mock", "llm"])


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="gia", description="Governed Iceberg Agent: reviewable, reversible agent actions on Iceberg (synthetic data).")
    ap.add_argument("--home", help="state directory (default $GIA_HOME or ./.lakehouse)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create local catalog + synthetic tables")
    p.add_argument("--rows", type=int, default=5000)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)

    sub.add_parser("profile", help="data-quality profile of bank.transactions").set_defaults(fn=cmd_profile)

    p = sub.add_parser("propose", help="ask the planner for plans (no execution)")
    p.add_argument("--goal", required=True)
    p.add_argument("--planner", default="auto", choices=["auto", "mock", "llm"])
    p.set_defaults(fn=cmd_propose)

    p = sub.add_parser("run", help="propose and run plans through policy, dry run, eval, approval")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--goal")
    g.add_argument("--plan-file")
    _add_approval_flags(p)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("rollback", help="restore the pre-change state of an executed plan")
    p.add_argument("plan_id")
    p.add_argument("--approver", required=True)
    p.add_argument("--force", action="store_true", help="roll back even if the table moved since (discards later changes)")
    p.set_defaults(fn=cmd_rollback)

    p = sub.add_parser("audit", help="show or verify the hash-chained audit log")
    p.add_argument("--plan")
    p.add_argument("--verify", action="store_true")
    p.set_defaults(fn=cmd_audit)

    p = sub.add_parser("history", help="Iceberg snapshot history with plan ids")
    p.add_argument("--table", default=TRANSACTIONS)
    p.set_defaults(fn=cmd_history)

    p = sub.add_parser("trace", help="span counts, or the span tree of one trace")
    p.add_argument("trace_id", nargs="?")
    p.set_defaults(fn=cmd_trace)

    p = sub.add_parser("docai", help="Document AI: generate synthetic docs or ingest with confidence routing")
    p.add_argument("action", choices=["generate", "ingest"])
    p.add_argument("--docs", default="docs_in")
    p.add_argument("--n", type=int, default=8)
    p.add_argument("--seed", type=int, default=7)
    _add_approval_flags(p)
    p.set_defaults(fn=cmd_docai)

    p = sub.add_parser("review", help="reviewer queue for low-confidence extracted fields")
    p.add_argument("action", choices=["list", "resolve"])
    p.add_argument("item_id", nargs="?")
    p.add_argument("--value")
    p.add_argument("--reviewer")
    p.set_defaults(fn=cmd_review)

    p = sub.add_parser("demo", help="end-to-end scripted walkthrough (offline)")
    p.add_argument("--interactive", action="store_true", help="prompt for approvals instead of using a scripted reviewer")
    p.set_defaults(fn=lambda a: __import__("governed_agent.demo", fromlist=["main"]).main(a))
    return ap


def main(argv=None):
    # PyIceberg warns when an overwrite on a fresh branch has nothing to delete; that is expected here.
    warnings.filterwarnings("ignore", message="Delete operation did not match any records")
    args = build_parser().parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
