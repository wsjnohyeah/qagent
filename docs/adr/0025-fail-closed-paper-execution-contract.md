# ADR 0025: Require a separately validated Paper execution contract

- Status: Accepted; amends ADR 0024
- Date: 2026-09-06

## Context

The current backtest and forward Shadow profile enters at the next session open, rechecks risk
at that open, and exits during the same bar. The initial Alpaca adapter instead submitted a
price-capped bracket that could enter later and remain open across sessions. Those are
different strategies, so a Shadow certificate cannot authorize the broker behavior.

Unknown submission outcomes, broker price increments, partial fills, and account changes also
mean idempotency alone is not sufficient authorization.

## Decision

1. Existing Shadow certificates cannot enroll for Paper. Paper requires the versioned profile
   `alpaca_day_limit_bracket_one_session@0.1.0`; no current validator issues it.
2. The adapter is therefore a read-only/reconciliation-capable foundation until a matching
   Paper backtest and complete position-exit lifecycle exist. This is an intentional
   fail-closed state, not a completed unattended-execution claim.
3. Every broker POST, including recovery of an unknown result, rechecks current enrollment,
   deployment, plan, execution profile, restriction, pinned account, buying power, and final
   rounded-price risk.
4. Orders use Alpaca `day` time-in-force. Price increments are deterministic and final prices
   are re-evaluated before submission.
5. A broker order is not a completed lifecycle while its symbol has a nonzero broker
   position. An expired partial entry cancels the remaining parent; an open position enters a
   durable manual-exit-required state and blocks expansion.
6. Forward Shadow supports `1Day` only. A plan is executable only after a post-commit
   activation check proves it became durable before the next session open.

## Consequences

- A paused production deployment and read-only broker probe remain valid deployment work.
- The first Paper order is blocked by code until the correct execution profile can be
  produced and explicitly enrolled.
- Full nested-child persistence, deterministic session-close exit, and execution-aware
  validation remain required before unattended Paper operation.
- Historical Paper/Shadow evidence stays distinct and cannot be combined into one return
  series.
