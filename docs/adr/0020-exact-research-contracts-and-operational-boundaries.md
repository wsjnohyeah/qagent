# ADR 0020: Exact research contracts and recoverable Phase 6 operations

- Status: accepted after the 2026-09-06 fix verification.
- Decision: bind admission to an exact executable contract, align research labels and
  execution semantics, and make Phase 6 state recoverable across processes before any
  Phase 7 broker adapter is considered.

## Context

The independent fix verification reproduced look-ahead sizing, label and horizon mismatch,
overlapping ML evaluation, date-only SEC timing, weak citation support, report borrowing by
an untested strategy, incomplete shadow risk lineage, false-positive readiness, and missing
worker/outbox recovery contracts. It also found that token-centric UI limits were not useful
to the operator.

## Decision

- A static validation certificate names the exact strategy specification, feature-set
  version, backtest engine version, and cost model. Adaptive-selector evidence is a different
  subject and cannot authorize a static strategy.
- Buy-and-hold remains a literal multi-session research benchmark and is not shadow-
  deployable. Momentum and mean-reversion share one parameterized signal implementation with
  shadow.
- Opening size uses only completed decision-bar liquidity. ML labels use the next bar open to
  the declared future bar close, map snapshots to exact bars, and separate purged calibration,
  model-selection, and untouched final-evaluation partitions.
- Date-only SEC filings become available at provider receipt time. A factual LLM claim must
  supply an exact quote that exists in the cited source; interpretation belongs in the thesis.
- Every attempted shadow exposure persists `SignalCandidate`, `RiskDecision`, and optional
  `TradePlan` rows before its virtual order/fill lineage. The baseline policy checks account,
  loss, concurrent-risk, restriction, data, liquidity, expiry, geometry, and sizing gates.
- SQL runtime controls are authoritative across API and worker processes. Production uses a
  dedicated heartbeat-reporting worker; workflow jobs require leases and dependency
  completion.
- Ledger events and outbox rows commit atomically. Delivery is at least once with stable event
  IDs, expiring claims, bounded retries, and a visible dead-letter state.
- Required dependency health returning false is a readiness failure, not a ready response.
- LLM operator budgets use estimated USD only. Token counts remain diagnostic accounting.
  Confirmed limit changes preserve spend and recalculate utilization against the new cap.
- ML + LLM strategy generation emits only a constrained immutable research specification
  after evidence binding and adversarial critique. It has no adoption, sizing, or execution
  authority.

## Consequences

- Artifacts created under older feature/backtest contracts cannot silently authorize the new
  runtime and must be recomputed.
- Local bounded tests can validate workflow correctness, but cannot satisfy the Phase 5 alpha
  criterion or Phase 6 elapsed continuous-operation criterion.
- The completed-bar shadow simulator remains broker-free. Exchange-time order interaction,
  broker reconciliation, and paper credentials remain Phase 7.
- Production TLS, backup/restore, external monitoring, and secret delivery remain deployment
  gates requiring infrastructure decisions rather than being claimed from local tests.
