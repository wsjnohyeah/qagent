# ADR 0009: Rolling walk-forward validation and selection diagnostics

- Status: accepted for Phase 3C baseline.
- Decision: evaluate strategy candidates with chronological rolling train/test windows,
  require an embargo, prohibit overlapping test windows, and retain all candidate runs.

## Context

A single full-period backtest rewards overfitting and cannot show whether a strategy selected
on earlier data survives later data or different regimes. Reporting only the winning
candidate also hides the multiple-testing process.

## Consequences

- Every fold has an explicit training interval, embargo, and later out-of-sample interval.
- All candidates run on both train and test data. Selection uses only the declared training
  metric; test performance cannot influence the selected strategy.
- Each fold links every underlying immutable experiment, records the selected strategy's
  out-of-sample rank, and labels the realized test regime as up, down, or sideways.
- Reports include compounded/mean selected out-of-sample return, mean out-of-sample Sharpe,
  worst drawdown, positive-fold rate, train-to-test Sharpe degradation, selection switches,
  and the rate at which the training winner falls below the test median.
- The below-median rate is an early, interpretable selection-risk diagnostic. It must not be
  called formal Probability of Backtest Overfitting. CPCV/PBO and Deflated Sharpe remain a
  later milestone requiring a frozen candidate set and sufficient data.
- Synthetic results validate orchestration only. Short local samples are never promotion
  evidence, and this change adds no paper or live trading authority.
