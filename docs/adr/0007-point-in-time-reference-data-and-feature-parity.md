# ADR 0007: Point-in-time reference data and feature parity

- Status: accepted for Phase 3A.2.
- Decision: use exchange-session close times for daily-bar availability, persist corporate
  actions and historical universe membership with separate effective and availability
  timestamps, and persist offline/online feature parity checks.

## Context

Raw daily prices cannot be interpreted safely with a fixed UTC-day delay or a permanently
current symbol universe. Exchange holidays and early closes change when a complete bar is
knowable. Splits can create false returns, while current index constituents create
survivorship bias. An offline feature job can also leak future rows even when its online
counterpart is correct.

## Consequences

- An Alpaca `1Day` bar becomes available at its exact configured XNYS session close. A date
  that is not a session is rejected rather than guessed. Migration `20260904_0008` rewrites
  existing daily rows so a replay is not required to adopt the corrected timing semantics.
- Provider prices remain raw. The feature builder adjusts pre-split prices and volumes only
  for actions whose `effective_at` and `available_from` are no later than `as_of`.
- Corporate actions are immutable and content-addressed by a deterministic fingerprint.
- Universe membership is an effective-dated interval plus `available_from`, source, and
  source version. Historical selection cannot consult a membership learned later.
- A parity audit compares the hash produced from complete stored history with the hash from
  an online-equivalent as-of slice and persists the result.
- The fast Phase 3A baseline backtester fails closed when a known corporate action falls in
  its replay window. Split-adjusted features do not substitute for correct share/cash event
  processing in a portfolio simulator.
- Provider adapters for corporate actions and historical constituent datasets are still
  required. Until then, the contracts are exercised by bounded deterministic fixtures.

These rules strengthen research correctness without expanding trading authority. No broker
submission path is introduced.
