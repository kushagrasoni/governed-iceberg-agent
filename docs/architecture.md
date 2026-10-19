# Architecture: the evidence trail for agentic pipelines

This demo uses synthetic data and runs on a laptop. Section 4 describes what changes in a
production bank lakehouse built on Apache Polaris, Spark and Trino.

![architecture](architecture.png)

Diagram source: [architecture.mmd](architecture.mmd). To re-render:
`npx -p @mermaid-js/mermaid-cli mmdc -i docs/architecture.mmd -o docs/architecture.png -p docs/puppeteer.json -b "#0F172A" -s 2`
(`puppeteer.json` points at a local Chrome).

## 1. Components

| # | Component | Module | Evidence it leaves |
|---|---|---|---|
| 01 | **Documents & source tables**: synthetic loan applications (text "scans") and `bank.transactions` with seeded defects | `datagen.py`, `docai.generate_documents` | source + file SHA-256; table snapshot id + uuid |
| 02 | **Document AI**: extract fields, confidence score per field, route below-threshold fields to a reviewer queue | `docai.py` | per-field confidence; `review_queue.jsonl` (QUEUED / RESOLVED with reviewer) |
| 03 | **Agentic pipeline**: a planner (OpenAI-compatible LLM or offline mock) emits structured JSON plans; tools act (profile, dry run, eval, commit) | `planners/`, `plan.py`, `pipeline.py` | provenance record: model, version, prompt and response hashes plus full text, inputs; trace spans for every tool call |
| – | **Guardrails + pre-execution evals**: static policy, sandboxed transform, Iceberg branch or staging write, evals, blast-radius gate | `policy.py`, `lakehouse.py`, `executor.dry_run`, `evals.py` | `POLICY_EVALUATED` ×2, `DRY_RUN_COMPLETED`, `EVAL_COMPLETED` with every check |
| 04 | **Human approval gate**: agents propose, humans approve. Fails closed | `approval.py` | approver, role, channel, reason, timestamp, decision |
| 05 | **Iceberg lakehouse (on-prem)**: publish moves main to the evaluated snapshot, tag `approved-<plan>`, rollback pointer | `executor.publish`, `executor.rollback` | snapshot ids, snapshot summary `gia.plan_id`, tags, `EXECUTED` / `ROLLED_BACK` |
| – | **Observability**: hash-chained audit log (JSONL + Iceberg `governance.agent_audit`), OTel-style traces | `audit.py`, `tracing.py` | Lineage · Provenance · Traces · Approvals |

## 2. Flow of one plan

```
goal ──► planner ──► Plan JSON (validated; invalid → PLAN_REJECTED_INVALID)
           │ provenance: model, version, prompt sha256, inputs (snapshot id, doc hashes)
           ▼
     static policy ── DENY ──► BLOCKED (nothing written)
           ▼ ALLOW (tier = max(base, declared, escalations))
     dry run: DuckDB sandbox computes post-state ──► Iceberg branch wap_<plan> (data ops)
                                                     or staging table (schema / new table)
           ▼
     evals on what Iceberg stored + blast-radius gate ── fail ──► DISCARDED (branch removed)
           ▼ pass
     approval gate (named human; elevated for HIGH; auto only for LOW + non-regulated)
           │── reject / EOF ──► DISCARDED
           ▼ approve
     publish: main := evaluated snapshot, tag approved-<plan>   (refuses if main moved)
           ▼
     EXECUTED {pre_snapshot_id, executed_snapshot_id, rollback_pointer}
           ▼ (any time later)
     gia rollback <plan> ──► main := pre_snapshot_id (+ drop added columns / drop created table)
                              refuses if later commits would be lost, unless --force
```

Every arrow above is a trace span. Every box writes an audit record.

## 3. Controls

| Control | Mechanism | Fails |
|---|---|---|
| Allowlisted operations | `allowed_operations`; irreversible ops refused | closed (DENY) |
| No DROP / DELETE without elevated tier | DROP-family ops refused outright; `DELETE_ROWS` is HIGH and needs `elevated` | closed |
| Read-only, single-statement SQL | comment- and literal-aware keyword scan; DuckDB with external access disabled and config locked | closed |
| PII | derived tables: raw PII refs only inside `sha256`/`md5`, no `SELECT *`; output name and value scan; in-place: PII refs escalate to HIGH, and PII values must be unchanged | closed |
| Blast radius | >10% rows changed escalates to HIGH; >25% denied; >5% rows removed denied; append volume cap | closed |
| Risk tier floor | the planner can raise, never lower | n/a |
| Pre-execution evals | schema additive-only, not-null, DQ no-regression on all metrics, expected effect met, row-count rules | closed |
| Separation of duties | planner identities can't approve; named human required for MEDIUM/HIGH and for any regulated table | closed |
| What you approve is what ships | publish points main at the evaluated snapshot id; stale base refuses | closed |
| Reversibility | rollback pointer on every execution; LIFO safety check | closed (refuses) |
| Tamper evidence | SHA-256 hash chain over audit records; `gia audit --verify` | detects |

## 4. What changes in a production bank setting (Polaris, Spark, Trino)

* **Catalog: Apache Polaris (Iceberg REST).** Replace the SQLite `SqlCatalog` with a REST catalog
  (`GIA_CATALOG_NAME` loads one from `~/.pyiceberg.yaml`; this hook is not tested here). Polaris
  uses role-based access control: privileges are granted to catalog roles, and catalog roles are
  assigned to principal roles. `TABLE_WRITE_DATA` is what hands a client short-lived read+write
  storage credentials for a table
  ([Polaris 1.8.0 access control](https://polaris.apache.org/releases/1.8.0/managing-security/access-control/)).
  The securable objects are catalog, namespace, table, view and policy. There is no branch-level
  privilege, so a principal that can write to an audit branch can also write to `main`. The
  agent's principal should therefore get read on source namespaces and write only on a staging
  namespace. A separate, human-triggered publisher principal holds write on regulated tables and
  does the branch fast-forward and rollback.
* **Compute: Spark for writes, Trino for reads/evals.** The plan's `SELECT` becomes Spark SQL. Iceberg's
  Write-Audit-Publish in Spark writes to a branch when the table has `write.wap.enabled=true` and
  the session sets `spark.wap.branch`. Publishing is
  `CALL catalog.system.fast_forward('db.tbl', 'main', 'wap_<plan>')`
  ([branching docs](https://iceberg.apache.org/docs/latest/branching/#audit-branch),
  [Spark procedures](https://iceberg.apache.org/docs/latest/spark-procedures/)). Rollback is
  `CALL catalog.system.rollback_to_snapshot(...)`, which needs the target to be an ancestor of the
  current snapshot, or `set_current_snapshot(...)`, which does not. Trino can run
  the eval queries against the branch or snapshot (`FOR VERSION AS OF 'wap_<plan>'` or a snapshot
  id, per the [Trino Iceberg connector docs](https://trino.io/docs/current/connector/iceberg.html))
  so eval compute stays separate from write compute.
* **Policy** moves from a JSON file to a policy service (e.g. OPA, which Polaris can also call as
  an external policy decision point) owned by risk and controls, with
  change control on the policy itself. Its `policy_version` is already stamped into every provenance
  record.
* **Approvals** come from the change-management or ticketing system with SSO identity, not a CLI
  string. Maker-checker happens there. HIGH routes to a model-risk / data-governance approver group.
* **Audit** goes to WORM / immutable storage or the SIEM. The hash chain becomes defense in depth,
  not the primary control. The Iceberg audit table should be compacted, because every event is a
  commit in the demo.
* **Traces** go through the OpenTelemetry SDK to the bank's collector (the span records already
  use OTLP field names: `trace_id`, `span_id`, `parent_span_id`, `start_time_unix_nano`,
  `end_time_unix_nano`). LLM spans should follow the
  [OpenTelemetry GenAI semantic conventions](https://github.com/open-telemetry/semantic-conventions-genai),
  which now live in their own repository and are still at *Development* status, so expect
  attribute names to change. The demo's planner span sets only `gen_ai.request.model` and
  `gen_ai.response.id`.
* **Provenance** feeds the bank's AI/model inventory: one inventory entry for the agent (model,
  version, prompt template hash, tools), linked to per-action records.
* **Snapshot retention.** Rollback is only as long as snapshot retention. `expire_snapshots` must
  respect the rollback window and legal holds. That is why the agent may never run it.
* **Document AI** in production is OCR plus a layout model. Confidence thresholds are calibrated
  per field on labeled data, and the reviewer queue is a real case-management UI with
  dual review for sensitive fields.

## 5. Limitations

* Single process, single writer. Concurrency is only handled by the stale-base check at publish.
* The audit log is tamper-**evident**, not tamper-proof. Anyone with file access can rewrite the
  whole chain. Anchor the head hash externally (WORM, a ticket, a timestamping service) in real use.
* Approver identity and role are self-asserted CLI arguments. There is no authentication.
* PII detection uses column names, masking-function syntax and SSN/email regexes. The paren-walk
  SQL check is a heuristic, not a SQL parser. Use a real parser (e.g. sqlglot) and a data
  classification service in production.
* The DQ metrics are hand-written for the synthetic transactions table. Other tables get only
  schema, not-null and diff checks.
* The DuckDB transform reads the whole table into memory. This is fine for a demo and not for
  multi-TB tables, where the Spark path applies.
* Schema changes are staged in a separate table, not on a branch, because Iceberg schemas are
  table-wide. Rollback of `ADD_COLUMN` drops the column from the current schema.
* Rolling back a derived table drops it from the catalog but keeps the files (no purge).
* Unapproved dry-run snapshots stay in table metadata as unreferenced snapshots until snapshot
  expiry. This is useful as evidence but adds metadata growth.
* The LLM planner has only been tested against a local fake endpoint in this build. Real model
  behavior (quality of plans, refusal, prompt injection via data) has not been evaluated.
* The Document AI extractor is a regex over clean synthetic text, so its confidences are
  heuristic, not calibrated.
* The docker-compose path (MinIO + REST catalog + Spark/Trino) is **not** included. Docker was not
  available in the build environment, and shipping an untested compose file seemed worse than
  documenting the mapping above.
* Nothing here constitutes compliance with SR 26-2, NIST AI RMF or NIST AI 600-1. SR 26-2 explicitly
  places generative and agentic AI out of its scope (footnote 3).
