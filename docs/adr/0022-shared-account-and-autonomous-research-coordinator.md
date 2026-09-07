# ADR 0022: Shared virtual account and autonomous research coordinator

- Status: accepted
- Date: 2026-09-06

## Context

Per-strategy cash ledgers could each spend the same nominal capital and therefore did not
represent one portfolio. Collection, feature materialization, ML, LLM research, strategy
generation, validation, and shadow admission also existed as separate tools without a durable
coordinator. Local development must prove those contracts on bounded data while production
must be able to run them over multi-year data without changing the workflow.

## Decision

Use one `SHARED_MASTER` virtual account with strategy/symbol sleeves. Cash and open-plan risk
are reserved atomically at the account boundary. Settlements update the master cash/P&L while
sleeve deployments retain attribution. Risk limits are versioned account revisions changed
only through the existing two-step administrator confirmation flow; a revision changes the
validation contract and therefore requires new exact validation evidence.

Run research as a persistent, dependency-aware nine-stage DAG backed by `workflow_jobs`:
gap-repaired market data, bounded document refresh, features, ML training, ML forecast,
evidence-bound LLM analysis, constrained strategy generation, exact validation, and
shadow-adoption readiness. Missing data, disabled
paid research, failed statistical gates, and pending human approval are successful
`WAITING_*` outcomes, not infrastructure failures. Infrastructure exceptions retain bounded
attempt counts and lease fencing and resume on a later scheduler poll.

Production bootstrap must register an immutable environment identity, require PostgreSQL and
Redis, create governed list/account defaults idempotently, and force new exposure paused.
Development and production never share a database identity. Runtime data is rebuilt in the
production data plane rather than copied through Git.

## Consequences

- Multiple strategies can no longer double-spend independent virtual balances or concurrent
  risk limits.
- Editing account risk invalidates earlier execution contracts by design.
- The coordinator may automate research, but cannot promote an ML model, adopt a strategy,
  start shadow/paper execution, or bypass budget/risk controls.
- Paid coordinator stages default off. Enabling them is an explicit environment decision and
  all calls remain governed by per-workload USD limits.
- Production deployment is reproducible after secrets/infrastructure inputs exist, but cloud
  uptime, backup/restore, TLS, and empirical strategy evidence cannot be certified locally.

The 2026-09-06 extension from eight to nine stages is governed by ADR 0026. It adds document
refresh and full-window gap repair without changing the human promotion boundary.
