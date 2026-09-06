# Alpaca Paper trading runbook

## Safety boundary

Paper is an external simulated broker account. It is distinct from the internal Shadow
ledger and does not imply profitability or authorize live money. The adapter accepts only
`https://paper-api.alpaca.markets`; there is no live trading mode or live broker provider.

Keep these defaults until the account and enrollment are reviewed:

```dotenv
TRADING_MODE=shadow
PAPER_TRADING_ENABLED=false
ALPACA_PAPER_BASE_URL=https://paper-api.alpaca.markets
LIVE_TRADING_ENABLED=false
```

## Staged activation

Current state: steps 1–4 are available for a paused/read-only deployment. Step 5 intentionally
fails for existing Shadow certificates because no current validator emits the required
`alpaca_day_limit_bracket_one_session@0.1.0` execution profile. Do not bypass this gate.

1. Configure the Alpaca credentials in the ignored environment file or approved production
   secret store. Never place them in Git, logs, screenshots, or discussion posts.
2. Open **Paper trading** in Control Center and run **Test read-only connection**. Confirm the
   returned account status, paper cash/equity, and existing position count.
3. Resolve every unexpected existing position. The worker blocks new exposure when it sees a
   position not associated with a tracked open Paper lifecycle.
4. Complete Strategy validation/adoption and start an active Shadow deployment.
5. After the Paper-compatible validator and session-close lifecycle are implemented, review
   and confirm `paper.enroll` for that exact compatible deployment. This does not submit an
   order; it makes only future plans eligible.
6. Change the worker environment to `TRADING_MODE=paper` and
   `PAPER_TRADING_ENABLED=true`, restart it, and verify the `paper` heartbeat. Keep global
   new exposure paused.
7. Inspect the pinned broker account ID, account snapshot, risk limits, and enrollment. Then
   separately confirm global resume.

## Runtime behavior

- The Paper worker polls at `PAPER_POLL_SECONDS`.
- A frozen Shadow `TradePlan` becomes one durable Paper intent with a deterministic
  `client_order_id`.
- Before POST, and after every uncertain response, the worker queries Alpaca by that client
  ID. It never creates a replacement identity for the same plan.
- Entry is a whole-share DAY limit order capped at the validated plan price. Stop-loss and
  take-profit legs are attached as a bracket.
- The worker cancels an unfilled or partially filled entry remainder after plan expiry. A
  nonzero broker position keeps the lifecycle open as `POSITION_OPEN_REQUIRES_EXIT`, blocks
  expansion, and requires operator resolution. Automatic close is not yet implemented.
- New exposure requires an active exact contract, unblocked broker account, adequate buying
  power, account floor and daily-loss headroom, per-trade and portfolio-risk headroom, no
  unmanaged positions, enabled pipeline, and resumed global switch.
- Pause blocks submission only. Reconciliation continues so fills/cancels are not lost.

## Inspection

Use the Paper page or these authenticated routes:

```text
GET  /v1/paper/status
POST /v1/paper/probe
GET  /v1/paper/account
GET  /v1/paper/positions
GET  /v1/paper/enrollments
GET  /v1/paper/orders
GET  /v1/paper/events
GET  /v1/paper/runs
```

Every mutation is proposed through `/v1/actions` and confirmed separately. Relevant actions
are `paper.enroll`, `paper.pause`, `paper.resume`, `paper.retire`, `paper.tick`, and
`paper.cancel_order`.

## Incident handling

1. Confirm global pause. Do not assume pause cancels existing orders.
2. Inspect Paper orders, lifecycle journal, account, and positions.
3. If cancellation is required, review and confirm `paper.cancel_order` for the exact order.
4. If the broker account ID changes, enrollments become `ACCOUNT_MISMATCH`; old intents stay
   bound to the original account and cannot be forwarded. Verify and resolve the old account
   before creating any future enrollment.
5. Preserve all database rows and broker payload snapshots. Never repair an incident by
   deleting order history.

No Paper order should be submitted merely to test deployment. Use fixture tests and the
read-only account probe first; the administrator decides when to permit the first paper order.
