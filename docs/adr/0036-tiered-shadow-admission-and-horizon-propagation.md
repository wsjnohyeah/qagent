# ADR 0036: Tiered Shadow admission and horizon propagation

- Status: accepted for production research remediation.
- Decision: preserve the planned research horizon through every coordinator stage and split
  broker-free Shadow admission into `CANDIDATE` and `QUALIFIED` tiers without weakening the
  Alpaca Paper boundary.

> Amendment: ADR 0041 permits configured deterministic automatic admission to either Shadow
> tier; it does not change either tier's evidence threshold or Paper authority.

## Context

The coordinator stored `horizon_bars` in each durable job but reconstructed stage context
without it. Downstream ML training and validation therefore used their one-session fallback,
so a job group labeled 5 or 20 sessions could still produce a one-session model and strategy.

The statistical gate also divided profitable OOS folds by every calendar fold. Sparse rules
were penalized as if a no-trade window were a losing window: production reports had many real
trades, but roughly three quarters of their folds contained no trade. A failed strict gate
then left no safe way to collect genuinely forward evidence.

## Consequences

- Every coordinator stage reapplies the immutable job's `horizon_bars`; legacy jobs without
  that field are explicitly treated as one-session jobs. ML labels, forecasts, LLM context,
  generated holding period, validation windows, and Shadow execution remain horizon-bound.
- `research_gate@0.3.0` introduced separate calendar, active, and zero-trade fold accounting;
  ADR 0037's `research_gate@0.4.0` adds horizon-aware Candidate activity minima. OOS trade
  count remains explicit. Its stability ratio is the fraction of profitable *active* OOS folds;
  inactivity is visible but is no longer misclassified as a loss. Strict qualification still
  requires minimum total folds, active folds, trades, regimes, Deflated Sharpe, drawdown, and
  applicable selection-risk evidence.
- An exact static strategy may become eligible for human-reviewed `CANDIDATE` Shadow when it
  has the configured minimum OOS coverage/activity, positive cost-adjusted compounded OOS
  return, and bounded drawdown. Candidate eligibility ignores neither costs nor losses, but it
  does not claim statistical qualification.
- Candidate Shadow is broker-free, labeled observation-only, and requires the same exact
  strategy/report/contract/policy/search-count checks plus the same separate administrator
  adoption and deployment confirmations as Qualified Shadow.
- `strategy_adoptions.admission_tier` is durable lineage. Candidate deployments are rejected
  at the Alpaca Paper enrollment boundary even when their execution profile is otherwise
  compatible. Only `QUALIFIED` one-session deployments may proceed to a separate Paper review.
- Historical reports and mislabeled job results are not rewritten. The policy hash change
  makes old reports stale for new adoption; new coordinator cycles must create correct
  horizon-bound evidence.
- Neither tier enables live money or gives an LLM risk, sizing, adoption, or broker authority.
