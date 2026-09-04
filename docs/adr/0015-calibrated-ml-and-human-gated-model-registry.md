# ADR 0015: Calibrated ML and a human-gated model registry

- Status: accepted for Phase 5.
- Decision: train transparent logistic and boosted-stump baselines on point-in-time feature
  snapshots, evaluate them chronologically, and separate model serving status from strategy
  approval.

## Context

An ML model may contribute a forecast to LLM research, but good in-sample fit is not evidence
that the model or a strategy is safe. Local bounded data is sufficient to validate training,
serialization, inference, calibration, drift, and registry mechanics, but not enough to claim
statistical edge.

## Consequences

- Labels use only later snapshots whose availability is no later than the declared training
  cutoff. Split and cash-dividend effects between feature time and label time are included.
- Logistic regression and deterministic boosted stumps run through expanding chronological
  train/embargo/test folds. Model selection uses a later calibrated OOS holdout rather than
  training fit.
- Platt calibration is fitted on the first chronological half of OOS predictions and measured
  on the second. ROC AUC, Brier score, log loss, accuracy, ECE, and feature PSI are retained.
- Model artifacts are safe JSON with content hashes; executable pickle artifacts are not
  accepted. Forecasts retain model, feature snapshot, training cutoff, and creation lineage.
- A model becomes `CHALLENGER` only after deterministic ML thresholds pass and becomes
  `CHAMPION` only through an explicit human registry action. No LLM may promote a model.
- `CHAMPION` is only a serving designation. Any downstream strategy still must pass
  `research_gate@0.1.0` and later runtime risk controls before Phase 6 or paper use.
