# ADR 0043: Prioritize short horizons and reject outlier-dependent candidates

- Status: accepted
- Date: 2026-09-12

## Context

Equal autonomous allocation across 5, 20, 63, 126, and 252-session strategies produces
slow forward-learning cycles. A 252-session position can remain open for roughly a year, so
it cannot supply completed Shadow observations at the same rate as a tactical strategy. The
operator chose 1, 2, 5, 10, and 20 sessions as the primary research horizons while retaining
63, 126, and 252-session research at lower frequency.

The former Candidate Shadow gate required horizon-scaled activity, positive cost-adjusted
compounded OOS return, and bounded drawdown. That was insufficient protection against a small
sample or a single extreme winner. Trade win rate alone is also not a valid universal gate:
a low-win-rate strategy can have positive expectancy when its realized winners are larger than
its losses, while a high-win-rate strategy can be negative after occasional large losses.

## Decision

1. The supported immutable horizons are 1, 2, 5, 10, 20, 63, 126, and 252 XNYS sessions.
   ML labels, Research LLM context, generated specifications, replay, validation, and Shadow
   execution must retain the same horizon.
2. The deterministic 24-hour UTC scheduler assigns 21 hourly research slots to the core
   1/2/5/10/20-session set. It assigns one daily background slot to each of 63, 126, and 252
   sessions. All horizons remain eligible for durable backlog recovery.
3. A one-session strategy remains a daily-bar strategy: it decides from a completed prior
   session, persists a price-capped order before the next open, and exits no later than that
   next session's close. It is not represented as a minute-level or high-frequency strategy.
4. Candidate Shadow activity minimums are:

   | Horizon | OOS folds | Active OOS folds | OOS trades |
   | --- | ---: | ---: | ---: |
   | 1 | 6 | 4 | 30 |
   | 2 | 6 | 4 | 25 |
   | 5 | 6 | 4 | 20 |
   | 10 | 6 | 4 | 15 |
   | 20 | 5 | 3 | 10 |

   Existing lower-frequency long-horizon activity minimums remain, but they do not bypass
   the robustness requirements below.
5. Candidate Shadow additionally requires positive cost-adjusted compounded OOS return, a
   worst continuous OOS drawdown no worse than -20%, at least 50% positive active OOS folds,
   realized OOS profit factor of at least 1.10, and positive compounded OOS return after the
   single largest winning trade is removed from its fold. The profit-factor and largest-winner
   checks also apply to the strict Qualified gate, so strict qualification cannot bypass a
   Shadow-wide robustness invariant.
6. Validation records OOS trade win rate, its Wilson 95% confidence interval, profit factor,
   mean and median trade return, largest-winner concentration, and the return-without-best-
   trade sensitivity. Win rate is visible evidence, not a fixed universal threshold.
7. The policy is versioned as `research_gate@0.5.0`. Older reports cannot authorize a new
   Shadow sandbox. A flat deployment with a stale certificate enters normal revalidation;
   any already-open virtual position continues only its persisted deterministic exit contract.

## Consequences

- Production spends materially more research capacity on strategies that can accumulate
  forward evidence in days or weeks.
- A positive total caused by one extraordinary trade cannot enter Candidate Shadow.
- Candidate Shadow remains an experimental broker-free tier rather than a profitability claim.
- The stricter gate can temporarily reduce the number of active sandboxes while exact reports
  are regenerated. No threshold is relaxed merely to manufacture activity.
- Paper enrollment, Robinhood mutation tools, and live money are unchanged and remain outside
  this decision.
