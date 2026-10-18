# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-18

First public release. A working, offline demo of an LLM agent that proposes changes to Apache
Iceberg tables, where every change is reviewable and reversible. All data is synthetic.

### Added
- **Structured plans** with validation of untrusted planner output (`plan.py`).
- **Planners**: an offline rule-based mock planner and an OpenAI-compatible LLM planner
  (`--planner auto|mock|llm`); the LLM path is tested against a local fake endpoint only.
- **Guardrails** from a versioned policy file (`default_policy.json`): operation allowlist,
  refusal of irreversible operations, single read-only SQL, PII rules, risk-tier floor and
  blast-radius limits.
- **Write-Audit-Publish executor**: dry runs on an Iceberg branch or staging table, publish of
  the exact evaluated snapshot with an `approved-<plan_id>` tag, stale-base protection.
- **Pre-execution evals**: schema compatibility, not-null rules, DQ no-regression, expected
  effect, PII checks.
- **Human approval gate** that fails closed, with `standard`/`elevated` roles and a narrowly
  scoped `--auto-approve-low-risk`.
- **Rollback** with LIFO safety checks and `--force`.
- **Evidence**: hash-chained audit log mirrored to `governance.agent_audit`, OpenTelemetry-style
  trace spans, per-plan provenance records.
- **Document AI** ingestion with per-field confidence routing and a reviewer queue.
- `gia` CLI (`init`, `profile`, `propose`, `run`, `rollback`, `audit`, `trace`, `history`,
  `docai`, `review`, `demo`) and a scripted end-to-end `gia demo`.
- 40 pytest tests; architecture write-up and diagram; captured sample runs.
- GitHub Actions CI on Python 3.10 to 3.13 (tests plus an offline `gia demo` smoke test).
- README "Try it in 5 minutes" quickstart, a terminal demo GIF, and a dev container for
  one-click GitHub Codespaces.

[0.1.0]: https://github.com/kushagrasoni/governed-iceberg-agent/releases/tag/v0.1.0
