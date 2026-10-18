"""`gia demo` story mode: the same governed walkthrough as `gia demo --fast`, told in 8 chapters
with rich panels, tables and pass/fail chips. Every number shown is read back from the run itself
(audit log, traces, provenance records, Iceberg metadata); nothing on screen is canned."""
from __future__ import annotations

import json
import re
from pathlib import Path
from collections import Counter

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from . import docai
from .config import get_settings
from .executor import RollbackRefused
from .lakehouse import TRANSACTIONS, read
from .pipeline import LOANS, Governor
from .profiler import profile

REVIEWER = "reviewer.alex"      # synthetic names
ELEVATED = "mrm.officer.priya"

CHAPTERS = [
    ("Data and seeded issues", "Bank data is never clean. Here the defects are planted on purpose, so we can check the agent's work."),
    ("The agent proposes", "The agent can only suggest. A plan is a document a reviewer can read, not an action already taken."),
    ("Guardrails block risky plans", "Bank policy (no raw PII, nothing irreversible, limited blast radius) is enforced in code, not left to the prompt."),
    ("Branch dry-run and evals", "Every change is rehearsed on an Iceberg branch or staging table and tested on what was really written, before approval."),
    ("Human approval gate", "A named, accountable person signs off. Regulated tables always need one; high-risk changes need a senior role."),
    ("Document AI confidence routing", "Fields the extractor is unsure about go to a person, not straight into the loan book."),
    ("Evidence trail", "When an examiner asks who changed this, why, and on what basis, the answer is a lookup, not an investigation."),
    ("Rollback", "Any approved change can be undone to an exact earlier snapshot, and unsafe undos are refused."),
]

ACCENT = "cyan"
SHORT = ["Data", "Propose", "Guardrails", "Dry-run", "Approval", "Doc AI", "Evidence", "Rollback"]


def progress(idx: int) -> Text:
    """A one-line map of the 8 chapters with the current one highlighted."""
    t = Text(justify="center")
    for i, name in enumerate(SHORT):
        if i:
            t.append(" · ", style="grey42")
        if i == idx:
            t.append(f" {i + 1} {name} ", style=f"bold #11111b on {ACCENT}")
        else:
            t.append(f"{i + 1} {name}", style="green" if i < idx else "grey50")
    return t


def where(home) -> str:
    try:
        return "./" + str(home.resolve().relative_to(Path.cwd().resolve())) + "/"
    except ValueError:
        return "the --home / GIA_HOME directory"


def chip(label: str, kind: str) -> Text:
    # explicit hex colors so chips keep their contrast under any terminal theme
    style = {"pass": "bold #0b1a0f on #4ac26b", "fail": "bold #ffffff on #d1242f", "warn": "bold #1f1600 on #f2cc60",
             "info": "bold #ffffff on #1f6feb", "muted": "bold #ffffff on #57606a"}[kind]
    return Text(f" {label} ", style=style)


STATUS_CHIP = {"EXECUTED": ("EXECUTED", "pass"), "BLOCKED_BY_POLICY": ("BLOCKED", "fail"), "EVAL_FAILED": ("EVAL FAIL", "fail"),
               "REJECTED": ("REFUSED", "warn"), "ERROR": ("ERROR", "fail")}


def status_chip(status: str) -> Text:
    return chip(*STATUS_CHIP[status])


def friendly(detail: str) -> str:
    """First failed-check detail, trimmed for a table cell."""
    d = detail.split("; ")[0]
    d = re.sub(r"raw PII columns referenced: \[(.*?)\] \(allowed only inside .*\)",
               lambda m: "raw PII in the output (" + m.group(1).replace("'", "") + "); must be hashed", d)
    return d.replace(" on this platform; agents may only take reversible actions", "; agents may only take reversible actions")


def short(n, k: int = 8) -> str:
    s = str(n)
    return s if len(s) <= k + 1 else s[:k] + "…"


class Story:
    def __init__(self, args, console: Console | None = None):
        self.args = args
        self.c = console or Console(highlight=False)
        self.pause = bool(getattr(args, "pause", False))
        self.settings = get_settings(args.home)
        self.g = Governor(self.settings, print_fn=lambda s="": self.c.print(s, markup=False, highlight=False))
        self.sim = None if getattr(args, "interactive", False) else "yes"
        self.n = 0

    # ------------------------------------------------------------------ presentation helpers
    def wait(self, hint: str) -> None:
        if not self.pause:
            return
        try:
            self.c.input(f"[grey50]⏎  {hint}[/]")
        except (EOFError, KeyboardInterrupt):
            self.c.print()
            self.pause = False  # no stdin (CI, pipe): carry on without pausing

    def title_card(self, idx: int) -> None:
        title, why = CHAPTERS[idx]
        body = Group(
            Text(f"Chapter {idx + 1} of {len(CHAPTERS)}", style="grey62", justify="center"),
            Text(""),
            Text(title.upper(), style=f"bold {ACCENT}", justify="center"),
            Text(""),
            Text(why, style="italic", justify="center"),
            Text(""),
            progress(idx),
        )
        self.cur = idx
        if self.pause:  # slide mode: the card stays up while the chapter's work runs, then Enter shows the result
            self.c.clear()
            self.c.print("\n" * 6)
            self.c.print(Align.center(Panel(body, box=box.HEAVY, border_style=ACCENT, padding=(1, 2), width=min(104, self.c.width))))
        else:
            self.c.print()
            self._header(idx)

    def header(self) -> None:
        """Called by each chapter right before it prints its results."""
        if self.pause:
            self.wait("Enter to show the chapter")
            self.c.clear()
            self._header(self.cur)

    def _header(self, idx: int) -> None:
        title, why = CHAPTERS[idx]
        self.c.print(Rule(Text(f" {idx + 1}/{len(CHAPTERS)}  {title} ", style=f"bold {ACCENT}"), style=ACCENT))
        self.c.print(Text.assemble(("  Why it matters for a bank: ", "bold gold1"), (why, "italic")))
        self.c.print()

    def end_chapter(self, idx: int) -> None:
        nxt = f"Enter for chapter {idx + 2}: {CHAPTERS[idx + 1][0]}" if idx + 1 < len(CHAPTERS) else "Enter for the scorecard"
        self.wait(nxt)

    def run(self, goal, approver=REVIEWER, role="standard", auto=False, pairs=None):
        outs = []
        for plan, prov in (pairs if pairs is not None else self.g.propose(goal, "mock")):
            outs.append((plan, self.g.run_plan(plan, prov, approver=approver, approver_role=role,
                                               auto_approve_low_risk=auto, simulated_decision=self.sim)))
        return outs

    def recs(self, plan_id: str, event: str) -> list[dict]:
        return [r["payload"] for r in self.g.audit.records(plan_id) if r["event_type"] == event]

    # ------------------------------------------------------------------ the story
    def intro(self) -> None:
        if self.pause:
            self.c.clear()
            self.c.print("\n" * 4)
        body = Group(
            Text("governed-iceberg-agent", style=f"bold {ACCENT}", justify="center"),
            Text("the evidence trail for agentic pipelines", style="bold", justify="center"),
            Text(""),
            Text("An LLM agent proposes changes to Apache Iceberg tables. Guardrails, evals on a branch "
                 "and an approval gate decide; regulated tables always need a named human. "
                 "Every step is recorded, and every change can be undone.", justify="center"),
            Text(""),
            Text("offline · synthetic data · 8 short chapters", style="grey62", justify="center"),
        )
        self.c.print(Align.center(Panel(body, box=box.DOUBLE, border_style=ACCENT, padding=(1, 3), width=min(90, self.c.width))))
        self.wait("Enter to begin")

    def ch1_data(self) -> None:
        self.title_card(0)
        m = self.g.init(rows=5000, seed=42, force=True)
        self.before = profile(read(self.g.catalog.load_table(TRANSACTIONS)))
        self.header()
        self.c.print(f"  Built a local Iceberg lakehouse: [bold]bank.transactions[/] with [bold]{m['rows_total']:,}[/] synthetic rows "
                     f"(snapshot {short(m['snapshot_id'], 10)}).\n")
        t = Table(box=box.ROUNDED, header_style=f"bold {ACCENT}", expand=False)
        t.add_column("Data-quality issue")
        t.add_column("Planted", justify="right")
        t.add_column("Profiler finds", justify="right")
        t.add_column("Why a bank cares")
        rows = [("Non-canonical currency codes", "noncanonical_currency", "noncanonical_currency", "breaks FX and reporting"),
                ("Duplicate (replayed) rows", "duplicate_rows", "duplicate_txn_id", "double-counts money"),
                ("Future-dated transactions", "future_dated", "future_dated", "impossible; feed error"),
                ("Missing merchant category", "null_merchant_category", "null_merchant_category", "weak fraud signals"),
                ("Negative purchase amounts", "negative_purchase_amount", "negative_purchase_amount", "sign errors")]
        for label, seeded, found, why in rows:
            t.add_row(label, str(m.get(seeded, "-")), Text(str(self.before.get(found, "-")), style="bold gold1"), why)
        self.c.print(t)
        self.c.print("  [grey62]The profiler counts a few more than were planted: the generator's own data has some.[/]")
        self.end_chapter(0)

    def ch2_propose(self) -> None:
        self.title_card(1)
        goal = "standardize currency codes"
        self.currency_pairs = self.g.propose(goal, "mock")
        plan, prov = self.currency_pairs[0]
        self.header()
        self.c.print(f'  Goal given to the agent: [bold]"{goal}"[/]\n')
        p = Table.grid(padding=(0, 2))
        p.add_column(style="grey62", no_wrap=True)
        p.add_column()
        p.add_row("plan id", Text(plan.plan_id, style="bold"))
        p.add_row("operation", Text(plan.operation, style="bold magenta"))
        p.add_row("target", plan.target_table)
        p.add_row("risk tier", f"{plan.risk_tier} (declared by the agent; policy re-checks it)")
        p.add_row("rationale", plan.rationale)
        sql = " ".join(plan.sql.split())
        p.add_row("sql", Text(sql if len(sql) < 150 else sql[:147] + "...", style="green"))
        self.c.print(Panel(p, title="[bold]Proposed plan (JSON, abridged)[/]", border_style="magenta", box=box.ROUNDED))
        q = Table.grid(padding=(0, 2))
        q.add_column(style="grey62", no_wrap=True)
        q.add_column()
        q.add_row("planner / model", f"{prov.get('planner')} / {prov.get('model')}")
        q.add_row("prompt sha256", str(prov.get("user_prompt_sha256", ""))[:16] + "…")
        q.add_row("input snapshot", str(prov["inputs"][0]["snapshot_id"]))
        q.add_row("policy / code", f"{prov['policy_version']} / {prov['code_version']}")
        self.c.print(Panel(q, title="[bold]Provenance recorded before anything runs (NIST AI 600-1 style)[/]", border_style="grey50", box=box.ROUNDED))
        self.c.print("  [grey62]Offline rule-based planner. Set GIA_LLM_API_KEY to use a real LLM; its output takes the same path.[/]")
        self.end_chapter(1)

    def ch3_guardrails(self) -> None:
        self.title_card(2)
        t = Table(box=box.ROUNDED, header_style=f"bold {ACCENT}", show_lines=True)
        t.add_column("Request to the agent", ratio=4)
        t.add_column("Agent proposed", ratio=4)
        t.add_column("Result", no_wrap=True)
        t.add_column("Guardrail that fired", ratio=7)
        self.blocked = []
        for goal in ("export a marketing contact list of top spenders", "purge Q1 old history to save space", "drop the table and start over"):
            for plan, o in self.run(goal):
                self.blocked.append(o)
                blk = (self.recs(o.plan_id, "BLOCKED") or [{}])[-1]
                stage = "caught after a dry run on a branch" if blk.get("stage") == "blast_radius" else "caught before touching any data"
                t.add_row(f'"{goal}"', f"{o.operation}\n[grey62]→ {o.target}[/]", status_chip(o.status),
                          Text.assemble((friendly(o.detail), ""), (f"\n{stage}", "grey62")))
        self.header()
        self.c.print(t)
        self.c.print(f"  [bold]{sum(o.status == 'BLOCKED_BY_POLICY' for o in self.blocked)} of {len(self.blocked)}[/] risky plans stopped. "
                     "Each block is written to the audit log with the rule that fired.")
        self.end_chapter(2)

    def ch4_branch(self) -> None:
        self.title_card(3)
        self.safe = []
        self.safe += self.run(None, pairs=self.currency_pairs)
        self.safe += self.run("add an amount band column", auto=True)
        self.safe += self.run("build a daily summary derived table", auto=True)
        self.safe += self.run("remove duplicate transactions")
        self.fix = self.safe[0][1]
        self.dedup = self.safe[3][1]
        t = Table(box=box.ROUNDED, header_style=f"bold {ACCENT}", show_lines=True)
        t.add_column("Plan", no_wrap=True)
        t.add_column("Rehearsed on", no_wrap=True)
        t.add_column("Rows before → after", justify="right")
        t.add_column("Evals", no_wrap=True)
        t.add_column("Headline check")
        for plan, o in self.safe:
            dr = (self.recs(o.plan_id, "DRY_RUN_COMPLETED") or [{}])[-1]
            ev = (self.recs(o.plan_id, "EVAL_COMPLETED") or [{}])[-1]
            checks = ev.get("checks", [])
            npass = sum(c["passed"] for c in checks)
            where = "Iceberg branch" if dr.get("branch") else "staging table"
            diff = ev.get("diff", {})
            rows = f"{diff.get('pre_rows', '-'):,} → {diff.get('post_rows', '-'):,}" if diff else "-"
            head = next((c for c in checks if c["name"].startswith("expected_effect")), None)
            if head:
                headline = f"{head['name'].split(':', 1)[1]}: {head['detail'].split(' (')[0].replace('->', '→')}"
            else:
                head = next((c for c in checks if c["name"] in ("existing_columns_unchanged", "no_pii_values")), checks[0] if checks else None)
                headline = head["detail"] if head else "-"
            t.add_row(f"{o.operation}\n[grey62]{o.plan_id}[/]", where, rows,
                      chip(f"{npass}/{len(checks)} PASS", "pass" if ev.get("passed") else "fail"),
                      Text(headline, style="green" if head and head["passed"] else "red"))
        self.header()
        self.c.print(t)
        self.c.print("  Write-Audit-Publish: nothing reaches main until its evals pass and the approval gate says yes; each published change is tagged approved-<plan_id>.")
        self.end_chapter(3)

    def ch5_approval(self) -> None:
        self.title_card(4)
        self.fut_refused = self.run("delete future-dated rows")[0]
        self.fut_pair = self.run("delete future-dated rows", approver=ELEVATED, role="elevated")[0]
        self.fut = self.fut_pair[1]
        t = Table(box=box.ROUNDED, header_style=f"bold {ACCENT}")
        t.add_column("Change → target", no_wrap=True)
        t.add_column("Tier", no_wrap=True)
        t.add_column("Regulated", justify="center", no_wrap=True, min_width=9)
        t.add_column("Needs", no_wrap=True)
        t.add_column("Approver", no_wrap=True)
        t.add_column("Decision", no_wrap=True, min_width=10)
        tier_style = {"LOW": "green", "MEDIUM": "gold1", "HIGH": "red"}
        for plan, o in self.safe + [self.fut_refused, self.fut_pair]:
            a = (self.recs(o.plan_id, "APPROVAL_RECORDED") or [{}])[-1]
            rule = "auto OK" if a.get("channel") == "auto-low-risk" else a.get("required_role", "-")
            decision = status_chip(o.status)
            approver = a.get("approver") or "-"
            if o.status == "REJECTED":
                approver = Text.assemble(approver, (f"\n({a.get('approver_role')} role)", "grey62"))
            t.add_row(f"{o.operation}\n[grey62]→ {o.target}[/]", Text(o.risk_tier, style=f"bold {tier_style.get(o.risk_tier, '')}"),
                      chip("YES", "warn") if a.get("regulated_target") else chip("no", "muted"), rule, approver, decision)
        self.header()
        self.c.print(t)
        self.c.print("  [bold]ADD_COLUMN[/] was LOW risk and auto-approval was on, but it writes to a regulated table, so a person still signed.")
        self.c.print("  [grey62]Approvals come from a scripted reviewer here (channel demo-simulated). Try `gia demo --interactive` to approve yourself.[/]")
        self.end_chapter(4)

    def ch6_docai(self) -> None:
        self.title_card(5)
        g = self.g
        docs = self.settings.home / "docs_in"
        docai.generate_documents(docs, n=8, seed=7)
        threshold = float(g.policy["docai"]["confidence_threshold"])
        routed, pairs = g.docai_ingest(docs)
        first = self.run(None, pairs=pairs)
        n_ready, n_held = len(routed["ready"]), len(routed["held"])
        key = json.loads((docs / "_answer_key.json").read_text())
        t = Table(box=box.ROUNDED, header_style=f"bold {ACCENT}")
        t.add_column("Document")
        t.add_column("Field")
        t.add_column("Extracted text")
        t.add_column(f"Confidence (bar {threshold:.2f})", no_wrap=True)
        t.add_column("Reviewer's value")
        for item in g.queue.open_items():
            val = key[item["doc"]][item["field"]]
            g.queue.resolve(item["item_id"], str(val), REVIEWER)
            g.audit.append("REVIEW_RESOLVED", None, REVIEWER, g.queue.state()[item["item_id"]], None)
            conf = item["confidence"]
            filled = round(conf * 10)
            bar = Text.assemble(("━" * filled, "red" if conf < 0.5 else "gold1"), ("─" * (10 - filled), "grey35"),
                                (f" {conf:.2f}", "red" if conf < 0.5 else "gold1"))
            t.add_row(item["doc"].replace(".txt", ""), item["field"], Text(repr(item["raw"]), style="red"),
                      bar, Text(str(val), style="green"))
        routed2, pairs2 = g.docai_ingest(docs)
        second = self.run(None, pairs=pairs2)
        loans = read(g.catalog.load_table(LOANS))
        self.reviewed_rows = sum(1 for v in loans.column("reviewed_by").to_pylist() if v)
        self.docai_outs = first + second
        self.header()
        self.c.print(Text.assemble("  8 loan documents extracted   ", chip(f"{n_ready} clear the {threshold:.2f} bar", "pass"), "  ",
                                   chip(f"{n_held} held: low-confidence fields", "warn")))
        self.c.print()
        self.c.print(t)
        self.c.print(f"  {REVIEWER} resolves the queue; the held documents then pass through the same governed gate:")
        self.c.print(Text.assemble("  ", chip(f"{loans.num_rows} rows in bank.loan_applications", "pass"), "  ",
                                   chip(f"{self.reviewed_rows} carry reviewed_by", "info")))
        self.end_chapter(5)

    def ch7_evidence(self) -> None:
        self.title_card(6)
        g = self.g
        ok, msg = g.audit.verify()
        recs = g.audit.records()
        spans = g.tracer.read()
        tbl = g.catalog.load_table(TRANSACTIONS)
        refs: dict = {}
        for name, ref in tbl.refs().items():
            refs.setdefault(ref.snapshot_id, []).append(name)
        cur = tbl.current_snapshot().snapshot_id
        main_chain, sid = [], cur
        by_id = {s.snapshot_id: s for s in tbl.snapshots()}
        while sid in by_id:
            main_chain.append(by_id[sid])
            sid = by_id[sid].parent_snapshot_id
        ops = {r["plan_id"]: r["payload"]["plan"]["operation"] for r in recs if r["event_type"] == "PROPOSED"}
        snap = Table(box=box.SIMPLE_HEAD, header_style=f"bold {ACCENT}", pad_edge=False,
                     caption="* current main · approved changes are tagged", caption_style="grey62")
        snap.add_column("snapshot", no_wrap=True, min_width=10)
        snap.add_column("change", no_wrap=True, overflow="ellipsis")
        snap.add_column("Iceberg tag", no_wrap=True)
        shown = [s for s in main_chain if any(r.startswith("approved-") for r in refs.get(s.snapshot_id, []))
                 or s.parent_snapshot_id is None]
        for s in reversed(shown[:6]):
            summ = s.summary.additional_properties if s.summary else {}
            pid = summ.get("gia.plan_id")
            snap.add_row(Text(short(s.snapshot_id, 7) + (" *" if s.snapshot_id == cur else ""), style="bold" if s.snapshot_id == cur else ""),
                         ops.get(pid, "seed data") if pid else "seed data",
                         Text(f"approved-{pid}" if pid else "-", style="green" if pid else "grey50"))
        tr = Tree(Text(f"trace {self.fix.trace_id[:12]}… ({self.fix.operation})", style="bold"))
        fspans = g.tracer.read(self.fix.trace_id)
        kids: dict = {}
        for s in fspans:
            kids.setdefault(s["parent_span_id"], []).append(s)

        def walk(pid, node):
            for s in sorted(kids.get(pid, []), key=lambda x: x["start_time_unix_nano"]):
                walk(s["span_id"], node.add(Text.assemble((s["name"], "bold" if pid is None else ""), (f"  {s['duration_ms']:.0f} ms", "grey62"))))
        walk(None, tr)
        _, prov = self.currency_pairs[0]
        pv = Table.grid(padding=(0, 1))
        pv.add_column(style="grey62")
        pv.add_column()
        pv.add_row("model", str(prov.get("model")))
        pv.add_row("prompt", str(prov.get("user_prompt_sha256", ""))[:12] + "…")
        pv.add_row("input", short(prov["inputs"][0]["snapshot_id"], 10))
        pv.add_row("policy", prov["policy_version"])
        audit = Group(
            Text.assemble(chip("CHAIN INTACT" if ok else "CHAIN BROKEN", "pass" if ok else "fail"), f"  {len(recs)} records"),
            Text(f"each record hashes the one before it (head {msg.split('head=')[-1]})", style="grey62"),
            Text(f"mirrored to Iceberg table governance.agent_audit", style="grey62"),
        )
        left = Group(Panel(snap, title="[bold]Iceberg snapshots: bank.transactions[/]", border_style="grey50", box=box.ROUNDED),
                     Panel(audit, title="[bold]Hash-chained audit log[/]", border_style="green3", box=box.ROUNDED))
        right = Group(Panel(tr, title=f"[bold]Trace spans ({len(spans)} in {len({s['trace_id'] for s in spans})} traces)[/]", border_style="grey50", box=box.ROUNDED),
                      Panel(pv, title="[bold]Provenance: currency fix[/]", border_style="grey50", box=box.ROUNDED))
        grid = Table.grid(expand=True, padding=(0, 1))
        grid.add_column(ratio=6)
        grid.add_column(ratio=4)
        grid.add_row(left, right)
        self.header()
        self.c.print(grid)
        self.c.print(f"  [grey62]Explore it yourself: gia audit --verify · gia history · gia trace {self.fix.trace_id[:8]}…[/]")
        self.end_chapter(6)

    def ch8_rollback(self) -> None:
        self.title_card(7)
        g = self.g
        mid = profile(read(g.catalog.load_table(TRANSACTIONS)))
        t = Table(box=box.ROUNDED, header_style=f"bold {ACCENT}", show_lines=True)
        t.add_column("Step", justify="right")
        t.add_column("Undo request")
        t.add_column("Result", no_wrap=True)
        t.add_column("What happened")
        self.rollbacks = {"refused": 0, "done": 0}
        steps = [(self.dedup, "dedup first (older change)"), (self.fut, "the future-dated delete"), (self.dedup, "then the dedup")]
        for i, (o, label) in enumerate(steps, 1):
            try:
                r = g.rollback(o.plan_id, approver=ELEVATED)
                self.rollbacks["done"] += 1
                t.add_row(str(i), f"{label}\n[grey62]{o.plan_id}[/]", chip("RESTORED", "pass"),
                          f"back to snapshot {short(r['restored_snapshot_id'], 10)}")
            except RollbackRefused:
                self.rollbacks["refused"] += 1
                t.add_row(str(i), f"{label}\n[grey62]{o.plan_id}[/]", chip("REFUSED", "warn"),
                          "a later change sits on top; undoing would silently discard it (use --force after review)")
        after = profile(read(g.catalog.load_table(TRANSACTIONS)))
        p = Table(box=box.ROUNDED, header_style=f"bold {ACCENT}", title="bank.transactions data-quality profile", title_style="bold")
        p.add_column("Metric")
        for col in ("Start", "After changes", "After rollback"):
            p.add_column(col, justify="right")
        for metric, label in (("noncanonical_currency", "non-canonical currency"), ("duplicate_txn_id", "duplicate txn ids"), ("future_dated", "future-dated rows")):
            p.add_row(label, str(self.before[metric]), Text(str(mid[metric]), style="green"), str(after[metric]))
        self.header()
        self.c.print(t)
        self.c.print(p)
        self.c.print("  The currency fix stays; the delete and the dedup are undone, each back to an exact earlier snapshot.")
        self.end_chapter(7)

    def scorecard(self) -> None:
        g = self.g
        if self.pause:
            self.c.clear()
        ok, msg = g.audit.verify()
        recs = g.audit.records()
        ev = Counter(r["event_type"] for r in recs)
        spans = g.tracer.read()
        prov_n = len(list((self.settings.home / "provenance").glob("*.json")))
        t = Table(box=box.HEAVY_HEAD, header_style=f"bold {ACCENT}", min_width=64)
        t.add_column("What the run did")
        t.add_column("Count", justify="right")
        t.add_column("Status", no_wrap=True)
        t.add_row("Plans proposed (agent + Document AI)", str(ev["PROPOSED"]), chip("RECORDED", "info"))
        t.add_row("Executed after evals and approval", str(ev["EXECUTED"]), chip("EXECUTED", "pass"))
        t.add_row("Blocked by guardrails", str(ev["BLOCKED"]), chip("BLOCKED", "fail"))
        t.add_row("Refused at the approval gate (wrong role)", str(sum(1 for r in recs if r["event_type"] == "APPROVAL_RECORDED" and r["payload"]["decision"] == "REJECTED")), chip("REFUSED", "warn"))
        t.add_row("Low-confidence fields sent to a reviewer", str(ev["ROUTED_TO_REVIEW"]), chip(f"{ev['REVIEW_RESOLVED']} RESOLVED", "pass"))
        t.add_row("Rollbacks (unsafe one refused first)", f"{ev['ROLLED_BACK']}", chip(f"{ev['ROLLBACK_REFUSED']} REFUSED", "warn"))
        t.add_row("Audit records, hash chain verified", str(len(recs)), chip("INTACT" if ok else "BROKEN", "pass" if ok else "fail"))
        t.add_row("Trace spans / provenance records", f"{len(spans)} / {prov_n}", chip("SAVED", "info"))
        self.c.print()
        self.c.print(Rule(Text(" Scorecard ", style=f"bold {ACCENT}"), style=ACCENT))
        self.c.print()
        self.c.print(Align.center(t))
        self.c.print(Align.center(Text.assemble(chip("REVIEWABLE", "pass"), "   ", chip("REVERSIBLE", "pass"), "   ", chip("EVIDENCED", "pass"))))
        self.c.print()
        self.c.print(Align.center(Text(f"evidence written to {where(self.settings.home)}  ·  synthetic data  ·  `gia demo --pause` to present", style="grey62")))
        self.ok = ok

    def main(self) -> None:
        self.intro()
        for step in (self.ch1_data, self.ch2_propose, self.ch3_guardrails, self.ch4_branch,
                     self.ch5_approval, self.ch6_docai, self.ch7_evidence, self.ch8_rollback):
            step()
        self.scorecard()


def main(args, console: Console | None = None) -> None:
    Story(args, console).main()
