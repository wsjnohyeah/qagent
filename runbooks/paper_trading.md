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

Current state: the validator, Shadow simulator, and broker runtime emit and require the same
`next_session_day_limit_bracket_moc@0.1.0` contract. Certificates from any older profile fail
closed and must be regenerated; never edit a certificate to bypass this check.

1. Configure the Alpaca credentials in the ignored environment file or approved production
   secret store. Never place them in Git, logs, screenshots, or discussion posts.
2. Open **Paper trading** in Control Center and run **Test read-only connection**. Confirm the
   returned account status, paper cash/equity, and existing position count.
3. Resolve every unexpected existing position. The worker blocks new exposure when it sees a
   position not associated with a tracked open Paper lifecycle.
4. Complete Strategy validation/adoption and start an active Shadow deployment.
5. Review and confirm `paper.enroll` for that exact compatible deployment. This does not
   submit an order; it makes only future plans eligible.
6. Change the production environment to `TRADING_MODE=paper` and
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
- Twenty minutes before the exchange close, the worker cancels all still-active entry/bracket
  legs. After broker evidence says that group is quiescent, any remaining position receives
  one deterministic `cls` market exit. A missed/rejected MOC uses a distinct deterministic
  DAY market emergency exit; new entries stay blocked until the account is flat.
- Parent, target, stop, scheduled-close, and emergency-exit rows are normalized in
  `paper_order_legs`; raw nested broker snapshots remain in the append-only event journal.
- Only one open Paper lifecycle per symbol is allowed, preventing one strategy's forced exit
  from closing another strategy's position.
- Reconciliation reads the broker order first and then refreshes positions before deciding
  lifecycle completion. A fill arriving after the tick's initial account snapshot therefore
  cannot be misclassified as flat or become an unmanaged position on the next tick.
- New exposure requires an active exact contract, unblocked broker account, adequate buying
  power, account floor and daily-loss headroom, per-trade and portfolio-risk headroom, no
  unmanaged positions, enabled pipeline, and resumed global switch.
- Global pause, Paper pipeline pause, or enrollment pause blocks new entries only.
  Reconciliation and risk-reducing exits continue so fills/cancels are not lost.

## Inspection

Use the Paper page or these authenticated routes:

```text
GET  /v1/paper/status
POST /v1/paper/probe
GET  /v1/paper/account
GET  /v1/paper/positions
GET  /v1/paper/enrollments
GET  /v1/paper/orders
GET  /v1/paper/order-legs
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
   If any quantity has filled, the confirmed action also creates a deterministic DAY market
   exit rather than leaving an unprotected position.
4. If the broker account ID changes, enrollments become `ACCOUNT_MISMATCH`; old intents stay
   bound to the original account and cannot be forwarded. Verify and resolve the old account
   before creating any future enrollment.
5. Preserve all database rows and broker payload snapshots. Never repair an incident by
   deleting order history.

No Paper order should be submitted merely to test deployment. Use fixture tests and the
read-only account probe first; the administrator decides when to permit the first paper order.
