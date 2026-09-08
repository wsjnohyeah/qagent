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
3. The administrator confirms `strategy.adopt` with the explicit admission tier.
4. The administrator confirms `shadow.start` for one symbol. Its validated capital must
   match the shared virtual master account.
5. The shadow pipeline and global new-exposure control are enabled.

No override can convert a report that failed the selected tier into an adoption. A strict
rejection may still have independently computed Candidate eligibility; that is not an
override and does not claim qualification.

## Runtime semantics

- The scheduler polls at `SHADOW_POLL_SECONDS`; a confirmed `shadow.tick` runs the same path.
- The runtime entry point rejects a paused tick when there is no position to manage. If a
  multi-session position is already open, the tick continues only its deterministic stop,
  target, mark, and timed-exit processing; it cannot create new exposure.
- Only `ACTIVE` deployments are evaluated.
- A new deployment establishes its cursor at the current data edge; historical bars seed its
  first pending signal but are not replayed as pretend forward shadow results. Backtests own
  historical evaluation.
- The runtime needs at least 21 decision bars and one following execution bar.
- Each decision uses evidence available by the decision bar's `available_from` timestamp.
- Momentum and mean-reversion use their immutable `StrategySpec` parameters and one of the
  approved 1, 5, 20, 63, 126, or 252-session holding horizons. The literal buy-and-hold
  benchmark remains research-only because it has no finite deployable exit contract.
- A long signal becomes a persistent `SignalCandidate`, passes the deterministic baseline
  shadow risk profile, and becomes a `TradePlan` only on approval. Account floor, daily loss,
  concurrent risk, restriction, data health, liquidity, duplicate intent, expiry, reward/risk,
  and position sizing are evaluated before any virtual order is recorded.
- `SignalCandidate.created_at`, `RiskDecision.evaluated_at`, and
  `TradePlan.created_at` are actual runtime observation/approval/persistence times; the
  candidate `as_of` remains the market-information cutoff. A plan cannot fill unless it was
  durably persisted before the simulated market open. Delayed historical arrivals therefore
  cannot enter the forward P&L ledger.
- The plan uses `next_session_day_limit_bracket_moc@0.1.0`: a DAY limit capped at the
  completed decision-bar close, fixed stop/target geometry, and same-session close. The
  actual fill price triggers a second reward/risk and quantity check. An untouched limit is
  recorded as `DAY_LIMIT_NOT_FILLED`; a fill is closed by stop, target, or modeled MOC.
- Multi-session plans use `next_session_day_limit_bracket_timed_exit@0.1.0`. The next-session
  DAY entry is unchanged, while filled position state survives restarts and is marked on each
  completed daily bar until stop, target, or the immutable maximum holding session. Recorded
  splits adjust quantity and prices; recorded cash dividends flow into virtual cash.
- Research, Shadow, and Paper share the same price-increment rounding. When only a daily bar
  proves an intraday limit touch, replay never credits an ambiguous target print that may
  have occurred before entry; it uses a later stop or the close.
- Every deployment is an attribution sleeve under one `SHARED_MASTER` virtual account.
  Approved plans atomically reserve account cash and risk; fills/cancellations release the
  reservation, and realized P&L settles once into the master account. Strategies therefore
  cannot each spend a duplicate copy of the same capital.
- Account limits are immutable revisions changed through `account.risk.update`. A new risk
  revision changes the execution contract, so old validation certificates fail closed.
- Every flat active deployment rechecks that certificate before a tick. A mismatch moves it to
  `REVALIDATION_REQUIRED`, cancels any open plan without erasing account history, and cannot
  be resumed until a current exact validation has been adopted. An already-filled
  multi-session position continues under its persisted contract until a deterministic exit;
  code/config drift cannot strand it.
- Virtual fills model commission, half-spread, slippage, fixed impact, and maximum bar-volume
  participation through the deterministic event-driven portfolio engine. Quantity is fixed
  before the session from completed-bar evidence; execution-bar volume is used only to model
  whether that already bounded order could fill.
- Deployment/bar/event uniqueness and `last_processed_bar_time` make reruns idempotent.
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
confirmation endpoint. The `shadow-active` system list is synchronized with active symbols.

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
