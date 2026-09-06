# ADR 0019: Explicit fail-closed risk context and shadow pause enforcement

- Status: accepted after the 2026-09-05 design-handoff review.
- Decision: require every tactical risk evaluation to receive explicit, auditable external
  safety facts, and enforce the global new-exposure pause inside the shadow runtime entry
  point as well as in its scheduler.

## Context

The original design handoff requires the deterministic risk boundary to fail closed when
catalyst verification, restricted-security status, liquidity, market-data health, macro
calendar state, or duplicate-order state is unknown or unsafe. The initial synthetic vertical
slice inferred several of those facts and recorded only the final decision. Separately, the
background shadow scheduler honored the global pause, but a manually confirmed `shadow.tick`
could call the runtime directly while paused.

The current Phase 6 runtime is still a broker-free daily-bar validation harness. It does not
yet claim to be the complete candidate-to-risk-to-approved-plan runtime described by the
target architecture.

## Decision

- `RiskEvaluationContext` is a required typed input to `evaluate_candidate`.
- Risk evaluation rejects a candidate whose feature ID does not match the supplied snapshot,
  or whose signal/feature timestamp is later than the evaluation time.
- Event strategies fail closed when their catalyst is not verified.
- Unknown restriction status, unconfirmed liquidity, unhealthy market data, an unknown macro
  calendar state, a duplicate order intent, or an applicable major-macro-event blackout all
  produce deterministic rejection reason codes.
- The versioned risk policy defines the blackout duration; the initial value is 24 hours.
- Only a separately validated macro-event strategy may bypass that blackout flag. An LLM
  cannot set or override any of these facts.
- The complete context is included in the risk-decision ledger event.
- Every shadow tick must receive the current global pause state. A paused runtime rejects both
  scheduled and manually confirmed processing at the runtime boundary.

## Consequences

- Call sites cannot accidentally omit safety inputs by relying on permissive defaults.
- Risk rejections can distinguish missing evidence from an explicitly unsafe condition.
- The global pause is defense in depth rather than only a scheduler convention.
- A future macro-calendar adapter must resolve and persist the nearest qualifying event before
  a runtime candidate can pass the gate.
- The next runtime milestone must replace the current daily-bar shadow harness with persisted
  candidate, risk-decision, approved-plan, and virtual-order lineage using this same contract.
