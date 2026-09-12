# Persistent shadow runtime runbook

## Purpose

The Phase 6 runtime validates the deployment workflow without submitting an order. It reads
only normalized bars already present in the database, materializes the same point-in-time
feature snapshots used by research, evaluates a versioned strategy, and appends virtual
portfolio events.

## Admission path

1. A strategy specification and validation report exist.
2. The deterministic report has subject `static_strategy`, binds the exact strategy ID,
   timeframe, feature version, engine version, cost/risk policy, current promotion-policy
   hash, and research-search count, and is eligible for the selected tier:
   - `QUALIFIED`: the strict report gate has `eligible_for_human_review=true`.
   - `CANDIDATE`: the candidate gate has `eligible_for_human_review=true`; this is
     observation-only and never Paper-eligible.
   Candidate activity minima are horizon-aware because a multi-year history can contain many
   independent one-session windows but only one annual window. Positive modeled net return,
   at least 50% positive active OOS folds, profit factor of at least 1.10, positive return after
   removing the largest winning trade, and the drawdown ceiling remain mandatory.
3. With `COORDINATOR_AUTO_SHADOW_ENABLED=true`, the deterministic coordinator adopts the
   strongest available exact tier and starts one isolated `$10,000` strategy/symbol sandbox.
   If disabled, the administrator may use the confirmation-gated manual path.
4. An explicit operator pause or retirement always blocks automatic reactivation. The
   validated capital must be exactly `$10,000` and its contract must match the sandbox policy.
5. The shadow pipeline and global new-exposure control are enabled.

No override can convert a report that failed the selected tier into an adoption. A strict
rejection may still have independently computed Candidate eligibility; that is not an
override and does not claim qualification.

## Runtime semantics

- The scheduler polls at `SHADOW_POLL_SECONDS`; a confirmed `shadow.tick` runs the same path.
- The runtime entry point rejects a paused tick when there is no position to manage. If a
  multi-session position is already open, the tick continues only its deterministic stop,
  target, mark, and timed-exit processing; it cannot create new exposure.
- `ACTIVE` deployments are evaluated for entries and exits. `LIQUIDATION_PENDING`
  deployments are exit-only until their next causally executable virtual fill.
- One deployment fault is persisted as `DEPLOYMENT_PROCESSING_FAILED` and makes that run
  `DEGRADED`; the runtime continues evaluating the other active deployments. A run is `FAILED`
  and contributes to worker restart protection only when every eligible deployment fails.
- A new daily deployment normally establishes its cursor at the current data edge; historical
  bars seed future signals but are not replayed as pretend forward results. One narrow exception
  is permitted when activation occurs after the latest daily bar became available and before its
  next exchange open: that latest completed bar is armed exactly once for a genuinely forward
  next-open decision. `PREOPEN_EVALUATION_ARMED` records this choice. Activation after the next
  open continues to skip that bar, so a missed entry can never be backdated.
- The runtime needs at least 21 decision bars and one following execution bar.
- Each decision uses evidence available by the decision bar's `available_from` timestamp.
- Momentum and mean-reversion use their immutable `StrategySpec` parameters and one of the
  approved 1, 2, 5, 10, 20, 63, 126, or 252-session holding horizons. The literal buy-and-hold
  benchmark remains research-only because it has no finite deployable exit contract.
- A long signal becomes a persistent `SignalCandidate`, passes the deterministic sandbox
  risk profile, and becomes a `TradePlan` only on approval. Restriction, data health,
  liquidity, duplicate intent, expiry, reward/risk, account floor, and position sizing are
  evaluated before any virtual order is recorded.
- `SignalCandidate.created_at`, `RiskDecision.evaluated_at`, and
  `TradePlan.created_at` are actual runtime observation/approval/persistence times; the
  candidate `as_of` remains the market-information cutoff. A plan cannot fill unless it was
  durably persisted before the simulated market open. Delayed historical arrivals therefore
  cannot enter the forward P&L ledger.
- The plan uses `next_session_day_limit_bracket_moc@0.1.0`: a DAY limit capped at the
  completed decision-bar close, volatility-derived stop/target geometry, and same-session close. The
  actual fill price triggers a second reward/risk and quantity check. An untouched limit is
  recorded as `DAY_LIMIT_NOT_FILLED`; a fill is closed by stop, target, or modeled MOC.
- Multi-session plans use `next_session_day_limit_bracket_timed_exit@0.1.0`. The next-session
  DAY entry is unchanged, while filled position state survives restarts and is marked on each
  completed daily bar until stop, target, or the immutable maximum holding session. Recorded
  splits adjust quantity and prices; recorded cash dividends flow into virtual cash.
- Research, Shadow, and Paper share the same price-increment rounding. When only a daily bar
  proves an intraday limit touch, replay never credits an ambiguous target print that may
  have occurred before entry; it uses a later stop or the close.
- Every immutable strategy/symbol deployment owns one isolated virtual account initialized at
  `$10,000`. Its planned risk is exactly 2% of current marked equity, recalculated for each
  signal and again at execution. It has no shared cash or concurrent-risk budget with another
  strategy. Portfolio allocation is deliberately outside Shadow and belongs to Paper.
- Stop distance is derived only from the point-in-time `realized_vol_20` feature, holding
  horizon, and strategy family. The result is clamped to 3%–15%; no stop may be wider than
  15%. The target is a versioned strategy-family R multiple. These parameters and the exact
  zero-commission cost model are part of the validation certificate.

  ```text
  raw stop = annualized realized_vol_20 / sqrt(252)
             × horizon multiplier × strategy multiplier

  horizon multiplier: 1=1.50, 2=1.75, 5=2.00, 10=2.35,
                      20=2.75, 63=3.50, 126=4.00, 252=4.50
  strategy multiplier: buy_and_hold=1.00, mean_reversion=0.90, momentum=1.10
  target R: buy_and_hold=2.00, mean_reversion=1.75, momentum=2.25
  ```
- A sandbox circuit breaker compares current cash plus marked unrealized P&L with `$8,800`.
  At or below the floor, no new entry is possible. A flat sandbox is immediately marked
  `RETIRED_SHADOW_FAILED`; an open sandbox becomes `LIQUIDATION_PENDING`, exits at the next
  causally executable virtual price, and is then retired. The immutable strategy version's
  adoption is also retired and cannot be automatically or manually re-adopted; research must
  generate and validate a new version.
- Every flat active deployment rechecks that certificate before a tick. A mismatch moves it to
  `REVALIDATION_REQUIRED`, cancels any open plan without erasing account history, and cannot
  be resumed until a current exact validation has been adopted. An already-filled
  multi-session position continues under its persisted contract until a deterministic exit;
  code/config drift cannot strand it.
- The research-search count is frozen into the administrator-approved validation certificate.
  New, unrelated experiments after adoption do not invalidate an active Shadow deployment;
  current policy, risk, cost, feature, restriction, and execution contracts still do.
- Virtual fills use zero explicit commission while retaining half-spread, slippage, fixed
  impact, gap behavior, and maximum bar-volume participation through the deterministic
  event-driven portfolio engine. Quantity is fixed
  before the session from completed-bar evidence; execution-bar volume is used only to model
  whether that already bounded order could fill.
- Deployment/bar/event uniqueness and `last_processed_bar_time` make reruns idempotent.
- Global or pipeline new-entry pause still invokes the runtime while a position is open. This
  is required for deterministic stop, target, mark, corporate-action, and timed-exit handling;
  the pause cannot strand exposure.
- Cash and realized P&L are virtual. This runtime has no broker SDK or submission call. The
  separate Phase 7 Paper runtime may mirror a newly approved plan only after an additional
  enrollment confirmation; it never converts Shadow history into broker history.

## Inspection

Use the Shadow page or:

```text
GET /v1/shadow/deployments
GET /v1/shadow/account
GET /v1/shadow/deployments/{id}
GET /v1/shadow/events?deployment_id={id}
GET /v1/shadow/runs
GET /v1/shadow/decisions?deployment_id={id}
GET /v1/shadow/reports?period=daily|weekly
GET /v1/shadow/alerts
GET /v1/runtime/controls
```

Every state-changing request is created through `/v1/actions` and finalized through the
confirmation endpoint. `shadow.migrate_to_sandboxes` cancels unfilled legacy plans, retires
flat legacy deployments, and places open legacy positions into deterministic liquidation.
The `shadow-active` system list is synchronized with active or liquidating symbols.

For a scheduled transition, set a timezone-aware boundary before deployment, for example:

```dotenv
SHADOW_NEW_EXPOSURE_NOT_BEFORE=2026-09-14T13:30:00Z
```

The boundary blocks only plans whose earliest execution would precede it. It does not block
risk-reducing exits. Keep the global pause on while running the migration action, verify that
`legacy_nonterminal_deployments` reaches zero, then resume the already approved Shadow-only
workflow. Starting sandboxes before the boundary is allowed; they cannot create early exposure.

## Known limitations

The runtime does not yet model multi-bar partial fills, cancellation, queue position,
quote-derived dynamic spread, delistings, or symbol-change replay. A symbol change during an
open virtual position fails for manual review. These limitations affect realism, not the
persistence, point-in-time, confirmation, or no-broker invariants.

The completed-bar simulator is a forward-workflow validation harness, not an exchange clock:
the next bar is already complete when its open/close are replayed. True order-time market
interaction and reconciliation belong to the separate Phase 7 Paper runtime; partial-fill
modeling remains limited. Phase 6's
continuous-operation observation period still requires elapsed runtime after deployment; it
cannot be replaced by a bounded local test.
