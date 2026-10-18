# governed-iceberg-agent

[![CI](https://github.com/kushagrasoni/governed-iceberg-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/kushagrasoni/governed-iceberg-agent/actions/workflows/ci.yml)

**The evidence trail for agentic pipelines.** This is a small, working demo of an LLM agent that
*proposes* changes to Apache Iceberg tables (fix a data-quality issue, add a column, build a
derived table, ingest extracted documents). It never gets to *just do* them. Each proposal is a
structured JSON plan. The plan goes through guardrails, runs on an Iceberg branch, passes
pre-execution evals, waits for a named human approver, and only then gets published. It can
always be rolled back. Every step leaves evidence: Iceberg snapshots for lineage, a provenance
record per action, OpenTelemetry-style trace spans for every tool call, and a hash-chained
audit log.

> Thesis: agents in a bank are fine when every action is **reviewable** and **reversible**.

Everything runs on a laptop: PyIceberg with a SQLite catalog on the local filesystem, DuckDB for
sandboxed transforms, no cloud keys, no Docker. **All data is synthetic** (see
[DATA_NOTICE.md](DATA_NOTICE.md)).

## Try it in 5 minutes

You need Python 3.10 or newer and git. Everything runs offline after the install: no cloud
account, no API key, no Docker.

1. Get the code:
   ```bash
   git clone https://github.com/kushagrasoni/governed-iceberg-agent.git
   cd governed-iceberg-agent
   ```
2. Create and activate a virtual environment:
   ```bash
   python3 -m venv .venv
   . .venv/bin/activate
   ```
3. Install the project (with the test tools):
   ```bash
   pip install ".[dev]"
   ```
4. Run the demo:
   ```bash
   gia demo
   ```
5. Optional, run the tests:
   ```bash
   pytest
   ```

In about 10 seconds the demo builds a small synthetic bank lakehouse, lets a rule-based agent
propose fixes, and prints each plan as `EXECUTED`, `BLOCKED` or `REJECTED` with who approved it,
followed by rollbacks and a summary of the audit log, traces and provenance records it wrote to
`./.lakehouse/`.

![architecture](docs/architecture.png)

## Why "evidence trail" and not "SR 11-7 compliance"

In 2011, U.S. model-risk guidance was SR 11-7 / OCC Bulletin 2011-12. On **April 17, 2026** the Federal
Reserve, OCC and FDIC replaced it with **SR 26-2 / OCC Bulletin 2026-13**, *Revised Guidance on Model
Risk Management*. SR 26-2 says it "supersedes and replaces SR letter 11-7", and the OCC bulletin
rescinds OCC Bulletin 2011-12. Footnote 3 of the revised guidance reads:

> "Generative AI and agentic AI models are novel and rapidly evolving. As such, they are not within
> the scope of this guidance. Nonetheless, a banking organization's risk management and governance
> practices should guide the determination of appropriate governance and controls for any tools,
> processes, or systems not covered in this document. However, the principles described in this
> guidance apply to traditional statistical and quantitative models and non-generative,
> non-agentic AI models."
> ([SR 26-2 PDF](https://www.federalreserve.gov/supervisionreg/srletters/SR2602.pdf), p. 3 of the guidance, footnote 3)

So no supervisory model-risk guidance covers the agent here. Its controls come from the bank's
own risk management and governance. This repo is a concrete starting point for those controls.
It borrows the long-standing model-risk *principles* (effective challenge, validation before use,
documentation) and pairs them with NIST AI RMF 1.0 and the NIST Generative AI Profile
(NIST AI 600-1). It does **not** claim compliance with anything.

## Commands

State goes to `./.lakehouse/` (override with `--home` or `GIA_HOME`). CI runs the tests and the
demo on Python 3.10, 3.11, 3.12 and 3.13 (Linux). Developed with PyIceberg 0.12.0 and DuckDB 1.5.

Individual commands:

```bash
gia init                                   # catalog + synthetic bank.transactions / bank.loan_applications
gia profile                                # data-quality metrics
gia propose --goal "remove duplicate transactions" --planner mock
gia run --goal "standardize currency codes" --approver your.name           # prompts y/N
gia run --goal "build a daily summary derived table" --auto-approve-low-risk
gia rollback <plan_id> --approver your.name
gia audit [--plan <plan_id>] [--verify]
gia trace [<trace_id>]                     # span counts, or one trace as a tree
gia history [--table bank.transactions]    # Iceberg snapshots with plan ids and refs
gia docai generate|ingest --docs <dir>     # Document AI with confidence routing
gia review list | resolve <item> --value V --reviewer NAME
```

### Using a real LLM

The planner works with any OpenAI-compatible `/v1/chat/completions` endpoint:

```bash
export GIA_LLM_API_KEY=...                       # or OPENAI_API_KEY
export GIA_LLM_BASE_URL=https://api.openai.com/v1 # or your internal gateway
export GIA_LLM_MODEL=gpt-4o-mini
gia run --goal "fix data quality" --planner llm --approver your.name
```

`--planner auto` (the default) uses the LLM when a key is set and falls back to the deterministic
rule-based **mock planner** otherwise. LLM output is treated as untrusted input. It is parsed,
validated, policy-checked and evaluated exactly like mock output. *This build's LLM path was
tested against a local fake OpenAI-compatible server (`tests/test_llm_planner.py`), not a live
model.*

## What the demo shows

Captured from a clean `git clone` → `pip install .` → `gia demo` run on 2026-10-18 at about
11:40 IST. Full output: [docs/sample-run/gia-demo.txt](docs/sample-run/gia-demo.txt).
Snapshot and plan ids differ per run. Audit timestamps are stored in UTC.

**1. Synthetic data with seeded defects**
```
seeded bank.transactions: 5000 rows, snapshot 7862520874184501590
profile: {'row_count': 5000, 'null_txn_id': 0, 'duplicate_txn_id': 75, 'noncanonical_currency': 202,
          'null_merchant_category': 150, 'negative_purchase_amount': 50, 'future_dated': 24}
```

**2–5. Safe changes go through Write-Audit-Publish**
```
[EXECUTED] p-1d82eccde8 FIX_DATA_QUALITY     tier=MEDIUM -> bank.transactions
           approved by reviewer.alex via demo-simulated
[EXECUTED] p-fe87153746 ADD_COLUMN           tier=LOW    -> bank.transactions
           approved by reviewer.alex via demo-simulated      <- LOW, but regulated table: auto-approve refused
[EXECUTED] p-31b6c55f82 CREATE_DERIVED_TABLE tier=LOW    -> analytics.daily_merchant_summary
           approved by auto-policy via auto-low-risk         <- LOW and non-regulated: auto-approved
[EXECUTED] p-04123b8222 DEDUPLICATE          tier=MEDIUM -> bank.transactions
```

**6–7. Guardrails stop what a careless agent proposes**
```
[BLOCKED ] CREATE_DERIVED_TABLE -> analytics.marketing_contacts
           raw PII columns referenced: ['customer_email', 'customer_name'] (allowed only inside ['sha256', 'md5'])
[BLOCKED ] DELETE_ROWS tier=HIGH -> bank.transactions
           32.45% of rows changed > hard limit 25.0%; 32.45% of rows removed > 5.0%
[BLOCKED ] DROP_TABLE tier=HIGH
           DROP_TABLE is irreversible on this platform; agents may only take reversible actions
[REJECTED] DELETE_ROWS tier=HIGH  HIGH requires role 'elevated', approver has 'standard'
[EXECUTED] DELETE_ROWS tier=HIGH  approved by mrm.officer.priya via demo-simulated
```

**8. Document AI with per-field confidence routing**
```
extracted 8 docs: ready=2 held=6 queued_fields=6
  held loan_app_002.txt: ['annual_income']        raw='$153,OOO'  conf=0.72
  held loan_app_003.txt: ['loan_amount']          raw='$1?,500'   conf=0.20
  held loan_app_004.txt: ['application_date']     raw='09/23/26'  conf=0.80
[EXECUTED] APPEND_RECORDS -> bank.loan_applications  (2 clean docs, human-approved: regulated table)
... reviewer resolves 6 fields ...
[EXECUTED] APPEND_RECORDS -> bank.loan_applications
bank.loan_applications rows: 8; reviewed rows: 6
```
Every loaded row carries `source_doc`, `source_sha256`, `extractor`, `min_confidence` and
`reviewed_by`.

**9. Reversibility (LIFO, with a safety check)**
```
rollback of p-04123b8222 refused: bank.transactions has moved on (current=5606…, expected=4458…);
  rolling back would also discard later changes. Re-run with --force after review.
rolled back p-b9ef06c862: restored_snapshot_id 4458795021177196158
rolled back p-04123b8222: restored_snapshot_id 736119098016682764
```

**10. Evidence**
```
audit log  : ./.lakehouse/audit/audit.jsonl -> 84 records, chain intact, head=d2b882199901
audit table: governance.agent_audit rows = 84
traces     : ./.lakehouse/traces/traces.jsonl -> 118 spans in 26 traces
provenance : ./.lakehouse/provenance (11 prompt records)
```

### The approval prompt (real run, piped answers)

From [docs/sample-run/interactive-approval.txt](docs/sample-run/interactive-approval.txt):

```
PLAN p-eeac65e75e  FIX_DATA_QUALITY  ->  bank.transactions
  sql       : SELECT * REPLACE (coalesce(merchant_category, 'UNCATEGORIZED') AS merchant_category) FROM source
  risk tier : MEDIUM (planner declared MEDIUM); regulated target: True
  dry run   : branch wap_p_eeac65e75e snapshot 7955417394091964119
  diff      : {'pre_rows': 5000, 'post_rows': 5000, 'removed_or_modified': 150, 'added_or_modified': 150, 'rows_changed': 150}
  checks    : operation_allowlisted=ok, ..., schema_compatible=ok, dq_no_regression=ok,
              expected_effect:null_merchant_category=ok, pii_values_unchanged=ok, blast_radius_hard_limit=ok, ...
Approve plan p-eeac65e75e [MEDIUM, REGULATED] as kushagra.s (standard)? [y/N]: Reason / ticket reference:
  [EXECUTED] p-eeac65e75e FIX_DATA_QUALITY     tier=MEDIUM -> bank.transactions
             approved by kushagra.s via interactive
```

The same plan's audit trail, trace and Iceberg history:

```
$ gia audit --plan p-eeac65e75e
 2 PROPOSED          mock-planner   FIX_DATA_QUALITY by rule-based-mock prompt=d5a6d87fbcca
 3 POLICY_EVALUATED  policy-engine  static: ALLOW tier=MEDIUM failed=[]
 4 DRY_RUN_COMPLETED executor
 5 EVAL_COMPLETED    evaluator      passed=True failed=[] diff={... 'rows_changed': 150}
 6 POLICY_EVALUATED  policy-engine  blast_radius: ALLOW tier=MEDIUM failed=[]
 7 APPROVAL_RECORDED kushagra.s     APPROVED by kushagra.s (interactive) at 2026-10-18T06:10:27+00:00
 8 EXECUTED          executor       bank.transactions 6130985874080627685 -> 7955417394091964119 rollback=restore_snapshot

$ gia trace bcb807318bd2aa64c5d3c7252fcc4411
agent.run_plan                   257.8 ms  OK
  guardrail.static_policy            1.4 ms  OK  {'decision': 'ALLOW', 'risk_tier': 'MEDIUM', 'failed': []}
  tool.dry_run                      54.9 ms  OK  {'staged_snapshot_id': 7955417394091964119}
  eval.pre_execution                63.2 ms  OK  {'passed': True, 'failed': []}
  guardrail.blast_radius             0.0 ms  OK  {'decision': 'ALLOW', 'risk_tier': 'MEDIUM'}
  human.approval_gate                0.1 ms  OK  {'decision': 'APPROVED', 'approver': 'kushagra.s'}
  tool.iceberg_commit                6.7 ms  OK  {'post_snapshot_id': 7955417394091964119}

$ gia history            # after `gia rollback p-eeac65e75e --approver ops.lead`
* 6130985874080627685  parent=-                    append  plan=-             stage=seed     refs=main
  5098696058921422348  parent=6130985874080627685  delete  plan=p-eeac65e75e  stage=dry-run  refs=
  7955417394091964119  parent=5098696058921422348  append  plan=p-eeac65e75e  stage=dry-run  refs=approved-p-eeac65e75e
```

After the rollback, the published snapshot still exists and keeps its `approved-<plan>` tag. Main
just points back at the seed snapshot. The record of what happened survives the undo.

Provenance record written for that plan (`.lakehouse/plans/<plan_id>.json`):

```json
{
 "planner": "mock-planner",
 "model": "rule-based-mock",
 "model_version": "governed-iceberg-agent 0.1.0 / rules sha256:a3d803685164c501",
 "system_prompt_sha256": "06b3dc7a…", "user_prompt_sha256": "d5a6d87f…", "response_sha256": "08dfbdd9…",
 "prompt_record": ".lakehouse/provenance/p-eeac65e75e.prompt.json",
 "inputs": [{"kind": "iceberg_table", "table": "bank.transactions",
             "snapshot_id": 6130985874080627685, "table_uuid": "8589d5e3-…"}],
 "policy_version": "2026.10-demo", "code_version": "0.1.0+3cb36ee"
}
```
With `--planner llm` the same record holds the provider's returned `model`, `system_fingerprint`
(as `model_version`), `response_id`, endpoint host and token usage.

## How it works (one paragraph per control)

* **Structured plans** (`plan.py`). A plan has an operation, target table, a single DuckDB `SELECT`
  over `source` that returns the full post-change table, a rationale, a declared risk tier and an
  `expected_effect` (e.g. `{"duplicate_txn_id": 0}`). Deletes are written as `SELECT … WHERE NOT (…)`.
  The agent describes the end state, and the platform computes the diff.
* **Guardrails** (`policy.py`, `default_policy.json`). Operations must be on an allowlist.
  Irreversible operations (`DROP_TABLE`, `DROP_COLUMN`, `TRUNCATE`, `EXPIRE_SNAPSHOTS`) are refused.
  `DELETE_ROWS` needs an *elevated* approver. SQL must be one read-only statement with no DDL/DML
  keywords. Derived tables may not carry raw PII, only `sha256()`/`md5()` of it. In-place changes
  that touch PII escalate to HIGH. Blast radius: more than 10% of rows changed escalates to HIGH,
  more than 25% is denied, and more than 5% of rows removed is denied. The final tier is
  `max(base, declared, escalations)`, so the agent can raise its own tier but never lower it.
* **Sandboxed transforms** (`lakehouse.sandboxed_sql`). DuckDB runs in memory with
  `enable_external_access=false` and a locked configuration. The only relations it can see are the
  ones passed in.
* **Dry run and evals** (`executor.dry_run`, `evals.py`). Data changes are written to an Iceberg
  branch `wap_<plan_id>`, and main is not touched. Schema changes and new tables go to a staging
  table, because an Iceberg schema is table-wide. Evals then run on what Iceberg actually stored:
  schema compatibility (additive only), not-null rules, DQ no-regression on every metric, the
  expected effect, PII values unchanged, an SSN/email value scan on derived outputs, and row-count
  rules per operation.
* **Human approval gate** (`approval.py`). It fails closed: no answer, EOF or no named approver means
  REJECTED. MEDIUM needs a named human. HIGH needs role `elevated`. `--auto-approve-low-risk`
  applies only to LOW tier plans that do **not** write to a regulated table. Planner identities can't
  approve their own plans. The audit log records approver, role, channel, reason and timestamp.
* **Publish = the evaluated snapshot** (`executor.publish`). For branch writes, main moves to the
  exact snapshot that was evaluated and gets an `approved-<plan_id>` tag. Nothing is recomputed. If
  main has moved since the dry run, publish refuses (`StaleBaseError`).
* **Rollback** (`executor.rollback`). Every EXECUTED event stores a rollback pointer: the
  pre-change snapshot (plus columns to drop for `ADD_COLUMN`), or the uuid of the table to drop for
  a derived table. Rollback refuses if later changes would be lost unless `--force` is given, and it
  refuses a second rollback of the same plan.
* **Audit** (`audit.py`). An append-only JSONL file (`O_APPEND` + fsync) where each record holds the
  SHA-256 of the previous one. `gia audit --verify` detects edits, deletions and reordering. Each
  record is mirrored into the Iceberg table `governance.agent_audit` so you can query it with SQL.
* **Traces** (`tracing.py`). Every tool call becomes a span with `trace_id`, `span_id`,
  `parent_span_id`, timings, attributes and status, using OpenTelemetry field names so the records
  can be mapped onto an OTLP exporter.
* **Provenance** (`pipeline._record_provenance`). Each plan records the planner, model and model
  version, prompt hashes, the full prompt and response file, its inputs (table snapshot id and uuid,
  staged batch hashes, document SHA-256s with per-field confidences), the policy version and the
  code version.
* **Document AI** (`docai.py`). A deterministic regex extractor scores confidence per field
  (OCR substitutions, smudged digits and ambiguous dates score low). Any field below 0.90 goes to
  an append-only reviewer queue, and the document waits. Clean documents, and documents whose
  fields a named reviewer has resolved, become one `APPEND_RECORDS` plan that goes through the same
  gate.

## Mapping to SR 26-2, SR 11-7 (historical), NIST AI RMF 1.0 and NIST AI 600-1

The rows below are design inspiration, not a compliance claim. SR 26-2 explicitly puts generative
and agentic AI out of scope (footnote 3, quoted above), and SR 11-7 has been superseded.

| Source (verified) | Concept | Where in this repo |
|---|---|---|
| SR 26-2 guidance §III | "Effective challenge": critical analysis by objective experts with "sufficient independence" and "standing and influence to effect any change" | `approval.py`: named human approver, `forbidden_approvers` (agents can't approve themselves), `elevated` role for HIGH; policy kept outside the agent in `default_policy.json` |
| SR 26-2 guidance §III | Risk management tailored, "with a level of rigor commensurate with that risk" | Risk tiers per operation, escalations (PII, blast radius), `tier_max` so the planner can't lower its tier |
| SR 26-2 guidance §V | "Validation generally occurs prior to a model's first use" | Pre-execution evals on an Iceberg branch or staging table; nothing publishes unless every check passes (`evals.py`) |
| SR 26-2 guidance §VI | Governance "supported by clear policies and effective controls"; roles and responsibilities | Versioned `default_policy.json` (`policy_version` stamped in every provenance record); `standard`/`elevated` roles |
| SR 26-2 guidance §VI (Documentation) | Documentation supports "tracking of recommendations, responses, and exceptions" | Audit events `PROPOSED`, `BLOCKED`, `APPROVAL_RECORDED`, `DISCARDED`, `ROLLBACK_REFUSED`, `ROLLED_BACK` |
| SR 11-7 attachment §V, Ongoing Monitoring (historical, 2011) | Code "subject to rigorous quality and change control procedures … all changes are logged and can be audited" | Hash-chained `audit.jsonl` + Iceberg snapshot summaries (`gia.plan_id`) + `approved-<plan>` tags |
| NIST AI RMF 1.0, GOVERN 1.4 | Risk management outcomes established "through transparent policies, procedures, and other controls" | Policy-as-data; every decision logged with the failing checks |
| NIST AI RMF 1.0, GOVERN 2.1 | Roles and responsibilities documented | Approver roles; approver identity in every approval record |
| NIST AI RMF 1.0, MAP 1.5 | "Organizational risk tolerances are determined and documented" | `blast_radius` thresholds, `max_rows_removed_pct`, `docai.confidence_threshold` |
| NIST AI RMF 1.0, MAP 3.5 | "Processes for human oversight are defined, assessed, and documented" | Approval gate rules; regulated tables always need a human; reviewer queue for low-confidence fields |
| NIST AI RMF 1.0, MEASURE 2.3 | Performance "demonstrated for conditions similar to deployment setting(s)" | Dry run on a branch of the *real* table; evals read back what Iceberg stored |
| NIST AI RMF 1.0, MEASURE 2.7 | Security and resilience evaluated | DuckDB sandbox (no file/network access), single-statement SQL, DDL/DML keyword block, LLM output validated as untrusted |
| NIST AI RMF 1.0, MEASURE 2.10 | Privacy risk examined and documented | PII column rules, masking-function requirement, SSN/email value scan, `pii_values_unchanged` eval |
| NIST AI RMF 1.0, MANAGE 1.1 | Determine "whether its development or deployment should proceed" | Each plan's go/no-go is an explicit, logged decision |
| NIST AI RMF 1.0, MANAGE 2.3 / 2.4 | Respond to and recover from risks; mechanisms to "supersede, disengage, or deactivate" | `gia rollback`, fail-closed approvals, `--planner mock` fallback |
| NIST AI RMF 1.0, MANAGE 4.1 | Post-deployment monitoring incl. "appeal and override, decommissioning, incident response, recovery" | Traces + audit log + rollback with `ROLLBACK_REFUSED` safety |
| NIST AI 600-1, GV-1.6-003 | Inventory entries should include "Data provenance information (e.g., source, signatures, versioning…)" and "versions of underlying models" | Provenance record: model, model_version / system fingerprint, endpoint, prompt hashes, input snapshot ids, document SHA-256s |
| NIST AI 600-1, MS-2.8-003 | Document "each instance where content is generated, modified, or shared to provide a tamper-proof history"; version control across the lifecycle | Hash chain + Iceberg snapshots/tags; `code_version` (git sha) on each plan |
| NIST AI 600-1, MG-2.2-002 | "Document training data sources to trace the origin and provenance of AI-generated content" (applied here to inference-time inputs) | `inputs[]` per plan; row-level `source_doc` / `source_sha256` on `bank.loan_applications` |
| NIST AI 600-1, Appendix A (Provenance) | Provenance metadata "can include information about GAI model developers or creators of GAI content, date/time of creation, location, modifications, and sources" | `planner`, `model`, `generated_at`, `inputs`, plus the stored prompt/response record |

Sources (each fetched and checked on 2026-10-18):
* Federal Reserve, **SR 26-2**, *Revised Guidance on Model Risk Management* (Apr 17, 2026):
  [letter](https://www.federalreserve.gov/supervisionreg/srletters/sr2602.htm) ·
  [PDF incl. guidance](https://www.federalreserve.gov/supervisionreg/srletters/SR2602.pdf) ·
  [guidance attachment](https://www.federalreserve.gov/supervisionreg/srletters/SR2602a1.pdf)
* OCC, **Bulletin 2026-13**, *Model Risk Management: Revised Guidance* (Apr 17, 2026), which rescinds OCC Bulletin 2011-12:
  <https://www.occ.gov/news-issuances/bulletins/2026/bulletin-2026-13.html>
* FDIC press release, *Agencies Issue Revised Model Risk Guidance* (Apr 17, 2026):
  <https://www.fdic.gov/news/press-releases/2026/agencies-issue-revised-model-risk-guidance>
* Federal Reserve, **SR 11-7** (Apr 4, 2011, superseded): [letter PDF](https://www.federalreserve.gov/boarddocs/srletters/2011/sr1107.pdf) ·
  [guidance attachment](https://www.federalreserve.gov/boarddocs/srletters/2011/sr1107a1.pdf)
* NIST, **AI RMF 1.0** (NIST AI 100-1, Jan 2023): <https://doi.org/10.6028/NIST.AI.100-1> ·
  [overview page](https://www.nist.gov/itl/ai-risk-management-framework). NIST notes the AI RMF 1.0
  "is being revised as part of the White House AI Action Plan".
* NIST, **AI 600-1**, *Generative AI Profile* (Jul 2024): <https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf>
* Apache Iceberg [branching and tagging](https://iceberg.apache.org/docs/latest/branching/) ·
  [Spark procedures](https://iceberg.apache.org/docs/latest/spark-procedures/) ·
  [PyIceberg API](https://py.iceberg.apache.org/api/) · [Apache Polaris](https://polaris.apache.org/) ·
  [Trino Iceberg connector](https://trino.io/docs/current/connector/iceberg.html)

## Repository layout

```
src/governed_agent/
  cli.py demo.py            CLI and scripted walkthrough
  plan.py                   plan schema + validation of untrusted planner output
  planners/{mock,llm}.py    offline rule-based planner; OpenAI-compatible planner
  policy.py                 guardrails, risk tiers, blast radius
  executor.py               Write-Audit-Publish on Iceberg branches / staging tables, rollback
  evals.py                  schema compat, DQ assertions, diff, PII checks
  approval.py               human approval gate
  audit.py tracing.py       hash-chained audit log; OTel-style spans
  docai.py                  synthetic documents, extraction, confidence routing, reviewer queue
  pipeline.py               the governor that wires it all together
  default_policy.json       the policy (owned by risk, not by the agent)
tests/                      40 pytest tests
docs/architecture.md        architecture, production mapping (Polaris/Spark/Trino), limitations
docs/architecture.mmd/.png  diagram source + render
```

## Tests

```bash
pip install -e ".[dev]" && pytest
# 40 passed in ~16 s
```

They cover the guardrails (allowlist, irreversible ops, DDL/DML in SQL, PII rules, tier floor,
blast radius, sandbox, expected effect, DQ regression), the approval gate (auto-approve scope,
self-approval, roles, interactive prompt, EOF fail-closed, rejected plans leaving main untouched),
the audit log (tamper and deletion detection, event sequence, snapshot linkage, Iceberg mirror, forced re-init archiving rather than deleting evidence),
provenance, tracing, rollback (snapshot restore, `ADD_COLUMN` and derived-table reversal, refusal
when the table has moved, `--force`, stale base), Document AI routing end to end, the LLM planner
against a fake endpoint (including a prompt-injected `DELETE`), and the real CLI with a piped
approval prompt.

## Limitations

See [docs/architecture.md](docs/architecture.md#limitations). In short: this is a single-process
demo. The audit chain is tamper-*evident*, not tamper-*proof*. Approver identity is a CLI string,
not SSO. The PII check is name-based plus a regex value scan, which is not a classifier. The
Document AI extractor is regex-based on synthetic text, not OCR. The optional REST/Polaris catalog
hook (`GIA_CATALOG_NAME`) is untested.

## License

MIT, see [LICENSE](LICENSE). Synthetic data only; see [DATA_NOTICE.md](DATA_NOTICE.md).
