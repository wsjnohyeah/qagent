# ADR 0006: Point-in-time research contracts and two-stage backtesting

- Status: accepted for Phase 3A.
- Decision: persist immutable evidence packets, feature snapshots, strategy
  specifications, experiment runs, and backtest trades with their as-of times, data/code
  hashes, costs, and lineage.
- Time rule: a feature may reference only records whose `event_time` and `available_from`
  are no later than the feature's `as_of`. A signal formed from a completed bar may fill no
  earlier than the next bar open.
- Validation rule: initial candidate evaluation uses a fast deterministic baseline runner;
  promotion evidence will later require event-driven replay, walk-forward/regime analysis,
  and explicit overfitting diagnostics. Synthetic smoke results validate infrastructure
  only and are never alpha evidence.
- Runtime rule: the same versioned `StrategySpec` and signal semantics should eventually run
  in backtest, shadow, and paper modes. Clock, data, and broker adapters may differ.
- Authority rule: LLMs and ML models may propose research artifacts. Only deterministic
  validation and an explicit promotion decision may move a candidate toward runtime; hard
  risk and execution controls remain independent.
- Baselines: Phase 3A starts with buy-and-hold, long/cash momentum, and long/cash mean
  reversion. They exist to test the research machinery and provide comparison points, not
  as claims of profitable strategies.
- Cost model: every backtest defaults to nonzero per-share commission, a minimum commission
  per order, and two-sided slippage. Overrides are recorded in the experiment.
- Rationale: a strategy-generation system is trustworthy only when every result is
  reconstructable from the exact evidence available at the decision time and when failed
  experiments are retained alongside successful ones.
