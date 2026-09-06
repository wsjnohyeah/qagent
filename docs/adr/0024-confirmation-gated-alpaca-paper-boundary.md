# ADR 0024: Use a confirmation-gated, idempotent Alpaca Paper boundary

- Status: Accepted
- Date: 2026-09-06

## Context

Phase 6 deliberately stopped at a broker-free forward Shadow harness. Phase 7 must exercise
real broker order state without creating any route to live money, replaying historical plans
as current orders, or allowing an LLM to submit an order.

A network timeout can occur after Alpaca accepts an order but before the worker receives the
response. Retrying a fresh order would duplicate exposure. Broker buying power and positions
can also differ from the internal virtual account, so validation alone is insufficient at the
submission boundary.

## Decision

1. The only supported broker endpoint is exactly
   `https://paper-api.alpaca.markets`. `live` is not a trading mode, and enabling Paper with
   any other host fails configuration validation.
2. Paper submission is separately armed by `TRADING_MODE=paper` and
   `PAPER_TRADING_ENABLED=true`; both default off for Paper. A human must also confirm a
   `paper.enroll` action for each exact active Shadow deployment and separately resume global
   new exposure.
3. An enrollment can consume only approved plans created after its confirmation timestamp.
   Historical plans cannot be back-submitted.
4. The worker persists an order intent before network I/O and derives one deterministic
   Alpaca `client_order_id` per plan. Every first attempt and retry looks up that ID. An
   unknown transport outcome remains nonterminal and is reconciled/retried with the same ID.
5. The first executable contract is long, whole-share, price-capped GTC bracket orders. The
   entry is a limit at the validated plan price; stop and take-profit are attached. An
   unfilled order is cancelled after plan expiry.
6. Before new submission, the runtime rechecks Alpaca account blocks, buying power, account
   floor, daily loss, per-trade risk, concurrent risk, account identity, exact strategy
   contract, and unmanaged positions. Any unknown or conflicting condition blocks new
   exposure.
7. Global pause prevents new orders but not account/order reconciliation or an explicitly
   confirmed cancellation. Broker observations are append-only; current account and
   positions are periodic snapshots.
8. Paper enrollment, pause/resume/retirement, manual ticks, and order cancellation use the
   existing expiring two-step administrator confirmation protocol. The LLM may propose these
   allowlisted actions but cannot confirm them.

## Consequences

- A process restart cannot turn one plan into two broker orders.
- Paper and Shadow results remain distinct evidence sources even though they share the same
  frozen plan lineage.
- The Paper account should be dedicated. Unmanaged positions block new automated exposure
  until the operator resolves them.
- A price-capped entry may miss a trade after an adverse gap. That is an intentional safety
  outcome, not an execution defect.
- The initial release does not support shorts, options, fractional shares, replacements,
  multi-strategy broker allocation, or live-money promotion.
