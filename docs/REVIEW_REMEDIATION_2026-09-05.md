# Review remediation status — 2026-09-05

## Scope

This document maps the independent `qagent_fix_verification_06b6853_2026-09-06.md`
findings to the implementation that follows commit `06b6853`. The review is evidence and a
repair checklist, not executable instructions. Current user decisions, safety policy, ADRs,
and tested code remain authoritative.

No broker adapter or live-money path was added. “Closed” below means the deterministic
counterexample is covered by code and regression tests; it does not claim profitability or a
completed production observation period.

## Quantitative correctness findings

| ID | Status | Remediation and evidence |
|---|---|---|
| F01 | Closed | Validation records distinguish static specs from selectors and bind the exact spec ID plus feature, engine, and cost contract. Shadow adoption rejects mismatches. |
| F02 | Closed | Buy-and-hold is again one literal multi-session benchmark and is marked research-only; the one-bar shadow executor cannot adopt it. |
| F03 | Closed for current shadow executor | Actual shadow candidates pass the deterministic baseline risk profile and persist candidate → risk → plan lineage. Account floor rejection is tested at the real shadow boundary. |
| F04 | Closed | Entry participation uses the completed decision bar's volume. Changing the future execution bar volume cannot resize the opening order. |
| F05 | Closed | ML labels measure the executable interval from next-bar open to the declared future-bar close. |
| F06 | Closed | Label horizons advance through actual market bars, never sparse snapshot positions. |
| F07 | Closed | OOS predictions are divided into purged calibration, model-selection, and untouched final-holdout partitions using label-availability times. |
| F08 | Closed | A date-only SEC filed value uses the observed provider receipt time as availability instead of invented midnight availability. |
| F09 | Closed for implemented gate | Cumulative selected-OOS drawdown and the complete historical strategy-trial count feed the gate. One-strategy PBO is explicitly not applicable rather than reported as evidence. |
| F10 | Closed | Every declared return and slow window selects a materialized versioned feature and changes the shared replay/shadow signal behavior. |
| F11 | Closed with a conservative rule | Every factual claim must equal an exact quoted span from every cited source; unsupported interpretation must remain in the thesis. Contradictory claims fail validation. |

The original counterexamples are regression tests in `tests/test_research.py`,
`tests/test_ml.py`, `tests/test_event_documents.py`, `tests/test_intelligence.py`, and
`tests/test_phase6_control_center.py`.

## Runtime-readiness findings

| ID | Status | Remediation or remaining boundary |
|---|---|---|
| R01 | Phase 6 scope implemented | Production Compose has a dedicated shadow worker and persistent SQL heartbeat. Collection/research schedules remain operator-defined production policy. |
| R02 | Closed | Workflow jobs have dependencies, atomic owner claims, expiring leases, heartbeat/cursor updates, stale recovery, and owner-only completion/failure. |
| R03 | Closed | Development may auto-migrate; every production API/worker/CLI process only starts at the exact Alembic head. |
| R04 | Closed as a safe research loop | A point-in-time ML forecast and evidence-bound analysis can be generated and adversarially critiqued into an allowlisted immutable strategy DSL. It remains research-only pending exact validation and human adoption. |
| R05 | Explicit Phase 6 limitation | The simulator is a completed-bar workflow harness, not an exchange clock. It prevents look-ahead inputs, but actual order-time interaction and partial execution require the Phase 7 paper adapter. |
| R06 | Closed at the ledger boundary | Ledger event and outbox insertion share one transaction. Stable IDs, lease recovery, retry, and dead-letter state provide at-least-once Redis delivery. Business tables retain their existing idempotency constraints. |
| R07 | Closed for Phase 6 | Runtime controls live in SQL and are shared across API/worker processes. New exposure is blocked at the runtime boundary. |
| R08 | Deliberate baseline boundary | Each strategy/symbol deployment has isolated virtual accounting. Portfolio-level candidate sleeves and cross-version continuity require a separately specified accounting model. |
| R09 | Closed for the requested LLM budget | Project/provider/workload admission uses estimated USD only. There is no token ceiling; token totals are optional diagnostics. Whole-system compute budgets remain an infrastructure concern. |
| R10 | Closed | Any required dependency returning false produces HTTP 503 readiness. Worker liveness is separately heartbeat-checked. |
| R11 | Closed for local release gating | `make release-check` runs static checks, all tests, local doctor, secret scan, Compose rebuild/doctor, and PostgreSQL schema-drift detection. |
| R12 | Code path implemented; remote proof pending | Production deployment starts migrations once, then separate API/worker services and verifies both. TLS, backup/restore, external monitoring, and secret delivery require the selected VPS and remain hard deployment gates. |

## Phase boundary after remediation

Phase 0–6.1 implementation is complete for the bounded local development contract. Two exit
criteria remain evidence-driven rather than coding tasks:

1. Phase 5 cannot claim out-of-sample alpha until production-scale data passes its statistical
   gate.
2. Phase 6 cannot claim stable continuous operation until the agreed real elapsed observation
   period and recovery drill have been run.

Phase 7 is unchanged and absent: no Alpaca trading-account client, paper-order submit,
broker position reconciliation, or live-money execution exists.
