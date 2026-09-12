# ADR 0035: Bind strategy research and Shadow execution to explicit holding horizons

- Status: accepted
- Date: 2026-09-08

The fixed 12.5% stop in decision 5 is superseded by ADR 0042. Multi-session Shadow now uses
versioned point-in-time volatility geometry with a hard 15% stop-distance ceiling.

## Context

The first deployable strategy contract was intentionally limited to a next-session entry and
same-session exit. That made replay, Forward Shadow, and Alpaca Paper comparable, but it did
not meet the product requirement to research both tactical day strategies and positions held
for days or months. Merely widening a stop would not create a multi-session strategy: the ML
label, LLM proposal, backtest, validation certificate, persisted position, and exit lifecycle
must all describe the same horizon.

## Decision

1. Generated strategies use one of six explicit maximum holding periods: 1, 5, 20, 63, 126,
   or 252 XNYS sessions. These represent same-day, weekly, monthly, quarterly, half-year, and
   annual research horizons.
2. The autonomous coordinator rotates through those horizons. ML labels and forecasts use the
   selected horizon; the Research LLM receives that exact horizon; the constrained generator
   must return the same `holding_period_sessions`. A mismatch fails instead of being coerced.
3. The horizon and position style are immutable `StrategySpec` data requirements. Validation
   contracts include the matching execution profile, backtest-engine version, price geometry,
   cost model, and risk policy. A certificate from one horizon cannot authorize another.
4. One-session strategies retain `next_session_day_limit_bracket_moc@0.1.0`. Multi-session
   strategies use `next_session_day_limit_bracket_timed_exit@0.1.0`: next-session DAY limit
   entry, protective stop/target on every held session, and deterministic close at the maximum
   holding session.
5. Multi-session strategies use a 12.5% price stop for the initial implementation. This does
   not increase account-dollar authority: the existing equity-fraction, per-trade dollar,
   concurrent-risk, daily-loss, and account-floor limits remain unchanged and reduce quantity
   when the stop is wider. LLM output cannot set stop, target, quantity, or account limits.
6. Forward Shadow persists the filled quantity, entry prices and commission, starting cash,
   scheduled exit, and applied corporate-action lineage. Open positions survive worker
   restarts. A global new-exposure pause continues deterministic risk-reducing exits while
   blocking new positions.
7. Multi-session Alpaca Paper enrollment remains fail-closed. Its safe contract requires a
   next-session-only entry expiry distinct from durable GTC protective exits and timed-exit
   recovery. The existing Paper lifecycle continues to accept only the separately validated
   one-session profile.

## Consequences

- Day, swing, and position strategies can be generated and compared without relabeling one
  execution contract as another.
- Long horizons need materially more history. A candidate may be generated but remain
  `WAITING_VALIDATION_HISTORY`, `INSUFFICIENT_EVIDENCE`, or `REJECTED`; no gate is weakened to
  make a long-horizon strategy visible.
- The 12.5% stop is a deterministic starting assumption for multi-session research, not a
  profitability claim or permission to lose 12.5% of the account on one trade.
- Research and broker-free Shadow now support multi-session positions. Paper support remains
  an explicit future implementation rather than an unsafe implicit reuse of DAY orders.
