# ADR 0008: Event-driven backtest portfolio ledger

- Status: accepted for Phase 3B baseline.
- Decision: replace direct round-trip arithmetic with a deterministic portfolio state
  machine and persist its ordered event stream.

## Context

Aggregate trade rows cannot explain how cash and share balances changed through a split or
dividend. They also hide the difference between a signal timestamp, an order decision, and
an exchange-session fill. This prevents trustworthy replay and makes future backtest/shadow
parity harder to establish.

## Consequences

- Every replay stores ordered `signal`, `order_submitted`, `fill`, `mark`, `split`, and
  `cash_dividend` events with resulting cash and position quantity.
- Daily entries use the next XNYS session open and daily exits use session close. One-minute
  bars retain their interval start and `available_from` close timestamps.
- Splits multiply held quantity without generating P&L. Cash dividends credit held shares;
  the trade summary records dividend cash and its final exit quantity.
- Costs include commission, slippage, and fixed market-impact assumptions on both sides.
  Entry quantity is capped by a configurable fraction of bar volume.
- If the final bar cannot liquidate the position within the configured participation cap,
  replay fails closed. Multi-bar partial fills are deferred rather than silently invented.
- Symbol changes fail closed until a cross-symbol market-data replay exists. A split learned
  after its effective time also fails because the intervening feature path cannot be made
  point-in-time correct.
- The engine remains a single-symbol, long/cash research baseline. It is not yet a complete
  exchange simulator and creates no broker connectivity or execution authority.
