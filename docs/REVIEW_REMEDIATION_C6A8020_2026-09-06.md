# Review remediation for `c6a8020`

Date: 2026-09-06 PDT

This document records the code-backed disposition of
`qagent_review_c6a8020_2026-09-06.md`. The review was treated as evidence to reproduce, not
as executable instructions. No real Alpaca Paper order was submitted while validating these
changes.

## Findings

| Finding | Disposition |
|---|---|
| R01 · unknown-submit retries bypass current authorization | Fixed. Every retry rechecks enrollment, deployment, plan, exact execution profile, restriction, account binding, buying power, per-trade risk, and total open risk. Enrollment changes share the broker-execution lease. Paused, retired, stale-contract, reduced-risk, canceled-plan, account-change, and global-pause regressions all produce zero new POSTs with a durable reason. A broker-accepted/lost-response case reconciles the same client ID without a second POST. |
| R02 · Shadow validation and Paper execution differ | Closed at the authorization boundary, not misrepresented as a complete Paper strategy. Existing `next_open_market_revalidated_bracket_one_bar@0.2.0` certificates cannot enroll or submit. Paper requires the distinct `alpaca_day_limit_bracket_one_session@0.1.0` profile, which the current validator deliberately does not issue. Building that matching backtest/lifecycle is still required before the first external Paper order. |
| R03 · Alpaca price precision | Fixed. Prices at or above `$1` use two decimals and sub-dollar prices use four. Long entry, target, and stop use explicit directional rounding; geometry and reward/risk are rechecked, and risk/buying-power checks use the final transmitted prices. |
| R04 · canceled parent incorrectly completed an open position | Unsafe completion fixed. Broker position quantity now participates in lifecycle completion; partial entry is canceled at expiry, but a nonzero position remains open as `POSITION_OPEN_REQUIRES_EXIT` and blocks new exposure. Position/fill mismatches also block. Parent lookup requests nested legs. Automatic session-close liquidation and full child-order persistence remain part of the R02-compatible execution engine, so unattended Paper remains gated. |
| R05 · coordinator abandons an older hourly group | Fixed. Every scheduler poll consumes older incomplete coordinator groups before the current group. A cross-hour regression proves the failed stage reaches attempt two while completed parents run only once. |
| R06 · computation start masquerades as durable plan time | Fixed. Risk uses decision-completed time. Plans enter `PENDING_ACTIVATION`, become `OPEN` only after a post-commit clock check before the next open, and otherwise cancel and release their account reservation. A computation crossing the open cannot create a late fill. |
| R07 · impossible 1Min forward timing | Fixed by explicit scope. Shadow adoption rejects non-`1Day` strategies until a future-execution minute contract and matching historical validator exist. |

The G03 cache concern is also corrected: validation reuse now requires the complete execution
contract plus a hash of the actual market bars, availability timestamps, and windowing inputs.
New/corrected data or engine, feature, cost, risk, restriction, capital, or window changes force
a new validation.

## Remaining product work

The review's G01, G02, G04, and G05 are roadmap/operational work, not silently declared fixed:

- broker-attributed Paper performance must become a versioned research input;
- historical-gap and document-refresh scheduling needs broader production coverage;
- ML-only versus ML+LLM incremental-value campaigns need untouched evaluation data;
- a real VPS still needs bootstrap, restart, backup/restore, TLS, monitoring, and elapsed-run
  evidence.

The source is suitable for a paused cloud bootstrap and read-only Alpaca Paper probe. It is
not authorized for unattended Paper submission until the separately validated Paper execution
profile, child-order lifecycle, and deterministic position-exit behavior are implemented and
tested.

## Subsequent C035 status

The later pre-deploy North Star review closes the locally actionable portions of G01 and G02:
the coordinator repairs internal/trailing daily gaps, refreshes bounded news evidence, and
feeds already-known backtest/validation/Shadow/Paper outcomes into later Research LLM evidence.
Licensed corporate-action/historical-universe refresh, controlled ablation evidence, real VPS
operations, and the Paper execution profile remain explicit gates. See
`PRE_DEPLOY_NORTH_STAR_REVIEW_2026-09-06.md` and ADR 0026.
