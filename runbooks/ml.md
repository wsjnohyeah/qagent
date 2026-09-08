# ML training and registry runbook

## Purpose

Phase 5 turns persisted point-in-time feature snapshots into research forecasts. It does not
approve a strategy or submit an order. Local bounded data validates the machinery; production
history is required for meaningful statistical conclusions.

## Workflow

1. Ingest and quality-check raw market/reference data.
2. Materialize point-in-time feature snapshots and verify offline/online parity.
3. Call `POST /v1/ml/train` with `symbol`, `timeframe`, `as_of_end`, and `horizon_bars`.
4. Inspect `GET /v1/ml/training-runs` and `GET /v1/ml/models`.
5. Call `POST /v1/ml/forecast` with a model and a feature snapshot at or after the model's
   training cutoff.
6. Pass the returned `forecast_id` to `POST /v1/intelligence/analyze`; the LLM must discuss
   and cite that forecast alongside the other evidence.

Run an isolated deterministic infrastructure smoke with:

```sh
make ml-smoke
```

For stored data, the equivalent CLI is `quant-research ml-train SYMBOL --timeframe 1Day
--end TIMESTAMP --horizon-bars 1`; use `ml-models` to inspect results and `ml-forecast` to
create a forecast from explicit model and feature-snapshot IDs.

The trainer compares logistic regression with boosted decision stumps using expanding
chronological folds and an embargo. It stores raw OOS metrics, calibrates on the first half of
OOS predictions, evaluates calibration on the later half, and measures PSI between older and
newer feature distributions.

Training reuse requires both the exact point-in-time dataset hash and
`walk_forward_ml_trainer@0.3.0` contract hash. The latter covers the complete ML policy,
feature set, label threshold/horizon, algorithms, purging/calibration method, and selection
rule. A policy change therefore retrains instead of silently returning an older model. The
Research LLM receives the selected model's label definition, final untouched-holdout metrics,
calibration, drift, gate result, and both dataset/training-contract identities with each
forecast.

Each walk-forward test fold is large enough to retain the configured minimum calibration and
model-selection samples after a full holding-horizon purge; the untouched final holdout still
uses the separate promotion minimum. A 252-session label therefore needs at least 1,092 labeled
rows under the default three-fold/20-calibration/20-selection policy. Production requests
approximately six calendar years of daily bars so mature issuers can meet that contract; later
listings wait explicitly rather than fabricating a model from overlapping outcomes.

## Promotion

`configs/ml_policy.yaml` owns minimum sample/OOS/fold counts and ROC-AUC, Brier, ECE, and PSI
thresholds. Bounded local samples are expected to remain `CANDIDATE`. A selected model becomes
`CHALLENGER` only if every deterministic ML check passes.

A human can promote an eligible challenger through
`POST /v1/ml/models/{model_id}/promote`, supplying `approved_by` and `reason`. This endpoint is
development-only until production authentication binds the approver identity. Every status
change is immutable in `model_registry_events`. There is no automatic or LLM promotion path.

Model-champion status is not strategy approval. A strategy consuming a model forecast must
still pass the separate `research_gate@0.4.0` walk-forward/PBO/DSR process.
