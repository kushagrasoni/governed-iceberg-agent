"""Scripted end-to-end walkthrough. Every step goes through the same governed path as the CLI."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from . import docai
from .cli import _print_outcome
from .config import get_settings
from .executor import RollbackRefused
from .lakehouse import TRANSACTIONS, read
from .pipeline import LOANS, Governor
from .profiler import profile

REVIEWER = "reviewer.alex"      # made-up names
ELEVATED = "mrm.officer.priya"


def rel(path) -> str:
    """Show paths under the working directory as ./..., so captured logs don't leak a home directory."""
    try:
        return "./" + str(Path(path).resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def h(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 66 - len(title)))


def main(args) -> None:
    settings = get_settings(args.home)
    g = Governor(settings)
    sim = None if args.interactive else "yes"

    def run(goal, approver=REVIEWER, role="standard", auto=False):
        outs = []
        for plan, prov in g.propose(goal, "mock"):
            o = g.run_plan(plan, prov, approver=approver, approver_role=role, auto_approve_low_risk=auto, simulated_decision=sim)
            _print_outcome(o)
            outs.append(o)
        return outs

    h("1. Init local Iceberg lakehouse (SQLite catalog + local files, generated data)")
    m = g.init(rows=5000, seed=42, force=True)
    print(f"  seeded bank.transactions: {m['rows_total']} rows, snapshot {m['snapshot_id']}")
    print(f"  seeded defects: { {k: v for k, v in m.items() if k not in ('rows_total', 'rows_clean_base', 'snapshot_id')} }")
    before = profile(read(g.catalog.load_table(TRANSACTIONS)))
    print(f"  profile: {before}")

    h("2. Safe, approved changes (Write-Audit-Publish on an Iceberg branch)")
    fix = run("standardize currency codes")[0]
    h("3. LOW tier but regulated table -> --auto-approve-low-risk does NOT apply")
    run("add an amount band column", auto=True)
    h("4. LOW tier, non-regulated analytics table -> auto-approved")
    run("build a daily summary derived table", auto=True)
    h("5. Deduplicate replayed transactions (MEDIUM)")
    dedup = run("remove duplicate transactions")[0]

    h("6. Guardrails: what a careless agent proposes")
    run("export a marketing contact list of top spenders")
    run("purge Q1 old history to save space")
    run("drop the table and start over")

    h("7. HIGH tier delete: standard reviewer is refused, elevated approver required")
    run("delete future-dated rows")
    fut = run("delete future-dated rows", approver=ELEVATED, role="elevated")[0]

    h("8. Document AI with per-field confidence routing")
    docs = settings.home / "docs_in"
    docai.generate_documents(docs, n=8, seed=7)
    routed, pairs = g.docai_ingest(docs)
    print(f"  extracted 8 docs: ready={len(routed['ready'])} held={len(routed['held'])} queued_fields={len(routed['queued'])}")
    for hld in routed["held"]:
        print(f"    held {hld['doc']}: {hld['open_fields']}")
    for plan, prov in pairs:
        _print_outcome(g.run_plan(plan, prov, approver=REVIEWER, simulated_decision=sim))
    print("  reviewer resolves the queue (simulated reviewer reads the generated answer key):")
    key = json.loads((docs / "_answer_key.json").read_text())
    for item in g.queue.open_items():
        val = key[item["doc"]][item["field"]]
        g.queue.resolve(item["item_id"], str(val), REVIEWER)
        g.audit.append("REVIEW_RESOLVED", None, REVIEWER, g.queue.state()[item["item_id"]], None)
        print(f"    {item['item_id']:<26} {item['field']:<17} raw={item['raw']!r:<12} conf={item['confidence']:.2f} -> {val!r}")
    routed, pairs = g.docai_ingest(docs)
    for plan, prov in pairs:
        _print_outcome(g.run_plan(plan, prov, approver=REVIEWER, simulated_decision=sim))
    loans = read(g.catalog.load_table(LOANS))
    print(f"  bank.loan_applications rows: {loans.num_rows}; reviewed rows: "
          f"{sum(1 for v in loans.column('reviewed_by').to_pylist() if v)}")

    h("9. Reversibility")
    try:
        g.rollback(dedup.plan_id, approver=ELEVATED)
    except RollbackRefused as e:
        print(f"  rollback of {dedup.plan_id} refused: {e}")
    r = g.rollback(fut.plan_id, approver=ELEVATED)
    print(f"  rolled back {fut.plan_id}: {r}")
    r = g.rollback(dedup.plan_id, approver=ELEVATED)
    print(f"  rolled back {dedup.plan_id}: {r}")
    after = profile(read(g.catalog.load_table(TRANSACTIONS)))
    print(f"  profile now: {after}")

    h("10. Evidence")
    ok, msg = g.audit.verify()
    recs = g.audit.records()
    spans = g.tracer.read()
    print(f"  audit log  : {rel(settings.audit_path)} -> {msg}")
    print(f"  audit table: governance.agent_audit rows = {read(g.catalog.load_table('governance.agent_audit')).num_rows}")
    print(f"  traces     : {rel(g.tracer.path)} -> {len(spans)} spans in {len({s['trace_id'] for s in spans})} traces")
    print(f"  provenance : {rel(settings.home / 'provenance')} ({len(list((settings.home / 'provenance').glob('*.json')))} prompt records)")
    print(f"  events     : {dict(sorted(Counter(r['event_type'] for r in recs).items()))}")
    print(f"  currency fix trace: gia trace {fix.trace_id}")
