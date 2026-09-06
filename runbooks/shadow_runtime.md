# Persistent shadow runtime runbook

## Purpose

The Phase 6 runtime validates the deployment workflow without submitting an order. It reads
only normalized bars already present in the database, materializes the same point-in-time
feature snapshots used by research, evaluates a versioned strategy, and appends virtual
portfolio events.

## Admission path

1. A strategy specification and validation report exist.
2. The deterministic report gate has `eligible_for_human_review=true` and covers the same
   strategy type/timeframe.
3. The administrator confirms `strategy.adopt`.
4. The administrator confirms `shadow.start` for one symbol and virtual cash amount.
5. The shadow pipeline and global new-exposure control are enabled.

No override can convert an insufficient/rejected validation report into an adoption.

## Runtime semantics

- The scheduler polls at `SHADOW_POLL_SECONDS`; a confirmed `shadow.tick` runs the same path.
- The runtime entry point itself rejects every tick while global new exposure is paused. This
  applies to scheduled and manually confirmed calls, preventing a controller bypass.
- Only `ACTIVE` deployments are evaluated.
- A new deployment establishes its cursor at the current data edge; historical bars seed its
  first pending signal but are not replayed as pretend forward shadow results. Backtests own
  historical evaluation.
- The runtime needs at least 21 decision bars and one following execution bar.
- Each decision uses evidence available by the decision bar's `available_from` timestamp.
- Momentum and mean-reversion use their immutable `StrategySpec` parameters. The baseline
  buy-and-hold spec is represented as repeated one-bar long exposure in the current shadow
  baseline; multi-session position lifecycle remains a later realism extension.
- Virtual fills model commission, half-spread, slippage, fixed impact, and maximum bar-volume
  participation through the deterministic event-driven portfolio engine.
- Deployment/bar/event uniqueness and `last_processed_bar_time` make reruns idempotent.
- Cash and realized P&L are virtual. There is no broker SDK, account endpoint, or order submit.

## Inspection

Use the Shadow page or:

```text
GET /v1/shadow/deployments
GET /v1/shadow/deployments/{id}
GET /v1/shadow/events?deployment_id={id}
GET /v1/shadow/runs
GET /v1/runtime/controls
```

Every state-changing request is created through `/v1/actions` and finalized through the
confirmation endpoint. The `shadow-active` system list is synchronized with active symbols.

## Known limitations

This baseline closes tactical positions within one execution bar and does not yet model
multi-bar partial fills, cancellation, queue position, quote-derived dynamic spread,
delistings, or symbol-change replay. These limitations affect realism, not the persistence,
point-in-time, confirmation, or no-broker invariants.

It also does not yet persist the complete tactical `SignalCandidate` → `RiskDecision` →
`TradePlan` chain for every shadow action. The standalone deterministic risk engine now has
an explicit fail-closed context contract, but wiring that contract into this baseline is the
next runtime milestone and is required before claiming the original Phase 6 exit criteria.
