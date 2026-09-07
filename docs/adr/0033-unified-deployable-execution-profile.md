# ADR 0033: Unify validation, Shadow, and Paper execution semantics

- Status: Accepted; supersedes ADR 0025
- Date: 2026-09-07

## Context

ADR 0025 correctly blocked Paper because historical validation assumed a market fill at the
next open while the broker adapter used a DAY limit bracket and had no automatic residual-
position exit. Enabling configuration in that state would have been cosmetic and unsafe.

Daily bars also cannot reveal whether a profit-target print happened before or after an
intraday limit entry. Alpaca rounds order prices to exchange-supported increments, so replay
and broker risk geometry must share that rounding.

## Decision

1. The only deployable profile is `next_session_day_limit_bracket_moc@0.1.0`.
2. A completed daily decision bar fixes a whole-share DAY buy limit at its conservatively
   rounded close, plus rounded stop and target prices. An opening price below the limit fills
   at the open; a later daily-bar touch fills at the limit.
3. Historical replay and Forward Shadow use stop-first ambiguity. After an intraday limit
   fill they never credit a target whose ordering is unknowable; absent a later modeled stop,
   they exit at the session close.
4. Paper uses Alpaca's bracket during the session. Twenty minutes before the known close it
   cancels the remaining parent/children, waits for quiescent broker state, and submits one
   deterministic market-on-close exit for the actual position.
5. A late or rejected scheduled exit creates a distinct deterministic DAY market emergency
   exit. Unknown responses recover by client ID. Closing exposure ignores the new-exposure
   pause but remains pinned to the exact broker account and observed quantity.
6. Parent, take-profit, stop-loss, scheduled-close, and emergency-exit orders are normalized
   durably. A lifecycle completes only with broker-flat evidence. One open lifecycle per
   symbol prevents attribution ambiguity.
7. The complete execution parameters are embedded in the hashed validation contract. Any
   older certificate becomes stale and cannot enroll for Paper.

## Consequences

- Backtest, Shadow, and Paper now test one named behavior rather than similar-looking but
  different strategies.
- MOC failure can carry a position into an emergency DAY exit; this is an incident path, not
  a claimed same-session result, and it blocks all new exposure while unresolved.
- Daily-bar validation remains conservative about intraday ordering. Quote/queue/partial-fill
  realism can improve later only under a new execution-profile version.
- Paper remains simulated external execution. No live-money mode or endpoint exists.
