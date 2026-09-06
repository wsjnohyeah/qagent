# ADR 0023: Subject-aware validation and open-price risk review

- Status: accepted
- Date: 2026-09-06

## Context

The fixed-strategy validation path inherited candidate-selection requirements: at least three
candidates and an applicable PBO statistic. Exact static validation necessarily contains one
frozen candidate, so it could never become eligible. Separately, the shadow plan used market
timestamps as creation timestamps and reused close-based sizing after a next-open gap. Active
deployments also checked their validation contract only at admission.

## Decision

- Give `static_strategy` and `adaptive_selector` distinct gate semantics. Static validation
  treats candidate breadth and PBO as not applicable, but keeps every absolute evidence and
  performance threshold. Selector validation still requires both.
- Feed Deflated Sharpe the conservative count of all recorded strategy-search attempts for
  the same symbol/timeframe contract, including rejected and failed hybrid attempts.
- Use actual runtime timestamps for candidate observation, risk approval and plan persistence.
  A bar-simulator fill is valid only when the plan was persisted before that market open.
- Treat the one-bar entry as a precommitted conditional market-on-open instruction. Preserve
  the bracket derived from the decision bar, then recalculate reward/risk and quantity at the
  actual open. Record that check as `EXECUTION_RISK_REVIEW`.
- Recheck an active deployment's exact validation contract before each tick. On mismatch,
  retain the full ledger but cancel pending exposure and enter `REVALIDATION_REQUIRED`.
- Resolve natural-key ingestion conflicts to the business ID already stored before emitting
  events. A legacy event with that business-object ID is reused and its missing outbox intent
  is repaired rather than creating a second logical event.

## Consequences

- The execution profile is `next_open_market_revalidated_bracket_one_bar@0.2.0`; the
  backtest engine is `event_driven_portfolio@0.4.0`; and the gate is
  `research_gate@0.2.0`. Prior certificates intentionally stop authorizing new exposure.
- Static strategies can reach human review without weakening fold, regime, drawdown,
  positive-OOS or Deflated Sharpe requirements.
- The completed-bar shadow runtime remains a Phase 6 workflow/evidence harness. True broker
  acknowledgements, partial fills and reconciliation remain Phase 7.
- Search trials are conservatively pooled by symbol/timeframe until explicit campaign
  isolation is implemented. This can make admission harder, not easier.
