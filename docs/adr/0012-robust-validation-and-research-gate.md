# ADR 0012: Robust validation and fail-closed research gate

- Status: accepted for Phase 3D.
- Decision: augment chronological walk-forward validation with combinatorial selection-risk
  analysis, Deflated Sharpe, and a versioned eligibility gate that never promotes a strategy.

## Context

Mean out-of-sample performance can still hide selection overfitting when many candidates are
tried. A short or regime-concentrated sample can also produce an impressive Sharpe that is not
credible evidence. Generated LLM/ML candidates need to enter a rejection system before any
research orchestration is connected.

## Consequences

- Every validation report evaluates combinatorial train/test selections over the already
  embargoed, non-overlapping OOS folds and reports Probability of Backtest Overfitting (PBO).
  Large combination sets are sampled deterministically and the evaluated/total counts remain
  explicit.
- Deflated Sharpe is calculated from non-overlapping OOS fold returns and records trial count,
  sample size, skewness, kurtosis, expected maximum Sharpe, and probability.
- `configs/research_promotion_policy.yaml` versions minimum total/active folds, OOS trades,
  candidates/regimes, PBO, DSR, positive-active-fold, and drawdown thresholds.
- The resulting status is `INSUFFICIENT_EVIDENCE`, `REJECTED`, or
  `ELIGIBLE_FOR_HUMAN_REVIEW`. `automatic_promotion` is always false.
- Synthetic and bounded local results validate the machinery only. They cannot satisfy the
  statistical meaning of a production-scale promotion decision merely by passing code tests.
- ADR 0036 refines sparse-strategy handling: no-trade folds remain explicit but are excluded
  from the positive-active-fold ratio, and a separately labeled candidate-only Shadow tier
  may gather broker-free forward evidence without satisfying the Qualified/Paper gate.
