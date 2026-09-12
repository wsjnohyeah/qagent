# ADR 0042: Isolate Shadow evaluation by strategy

Status: accepted

Date: 2026-09-11

## Context

The original Shadow runtime placed every strategy/symbol deployment under one shared virtual
master account. That design tested portfolio contention, but it prevented otherwise valid
strategies from opening when unrelated strategies consumed the shared `$780` risk ceiling. The
operator clarified that Shadow exists to evaluate each strategy independently; Alpaca Paper is
the appropriate place to observe portfolio-level capital allocation and broker behavior.

The operator also approved strategy- and stock-sensitive stop/target geometry, a 15% maximum
stop distance, zero explicit commission, and retirement when an isolated `$10,000` sandbox
falls to `$8,800`.

## Decision

1. Every new immutable strategy/symbol Shadow deployment owns a distinct virtual account with
   `$10,000` initial cash. No cash, reservation, loss, or risk budget is shared between strategy
   sandboxes.
2. Each planned position risks 2% of that sandbox's current marked equity. Quantity remains
   whole-share, liquidity-capped, cash-reserved, and rechecked at the execution boundary.
3. Stop distance is derived deterministically from the point-in-time annualized
   `realized_vol_20` value, immutable holding horizon, and allowlisted strategy family. It is
   clamped to 3%–15%. The corresponding target uses a versioned strategy-family R multiple.
   The formula and bounds are part of the exact validation execution contract.
4. Explicit equity commission is `$0`. Half-spread, slippage, market impact, volume
   participation, and gap behavior remain modeled because they are market execution effects,
   not broker commission.
5. Current sandbox value is cash plus marked unrealized P&L. At or below `$8,800`, the runtime
   blocks new exposure. A flat sandbox retires immediately. An open position enters
   `LIQUIDATION_PENDING`, exits at the next causally executable virtual price, and then retires.
   The failed immutable strategy version is retired and cannot be re-adopted; a changed idea
   requires a newly generated and independently validated version.
6. The former shared-account deployments are historical evidence only. A confirmed migration
   cancels their unfilled plans, retires flat deployments, and requests deterministic
   liquidation for open positions without rewriting prior events.
7. A timezone-aware `SHADOW_NEW_EXPOSURE_NOT_BEFORE` boundary may stage a rollout. It blocks
   only exposure whose earliest execution precedes the boundary and never blocks exits.
8. Database uniqueness permits historical terminal deployments while allowing at most one
   nonterminal deployment for a strategy/symbol pair.

## Consequences

- Shadow results answer “does this strategy survive and behave as specified?” without
  cross-strategy capital contention.
- The sum of all Shadow sandbox values is not a deployable portfolio and must not be presented
  as one. Portfolio allocation, correlated exposure, and broker reconciliation belong to Paper.
- Changing the risk geometry, cost model, or sandbox capital invalidates older exact validation
  certificates. Existing strategies must be revalidated before a new sandbox starts.
- The `$8,800` floor is a permanent failure decision for that immutable strategy version, not a
  daily loss limit or a temporary pause.
- This decision supersedes ADR 0022 only for Shadow capital/risk topology. ADR 0022's production
  environment isolation and durable coordinator decisions remain in force.
- No broker permission changes. Shadow remains broker-free, Paper remains separately gated,
  Robinhood remains read-only/preview-only, and `LIVE_TRADING_ENABLED=false` remains mandatory.
