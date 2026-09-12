# Point-in-time research runbook

Phase 3 provides an auditable research and event-driven replay vertical slice. It does not
authorize paper or live orders, and its deterministic synthetic results are infrastructure
checks rather than evidence of alpha.

## One-command health check

```sh
make research-smoke
```

This command uses the isolated, ignored `work/research-smoke.db`, creates 100 deterministic
daily bars, builds point-in-time evidence and feature snapshots, and runs buy-and-hold,
momentum, and mean-reversion baselines with zero explicit commission plus modeled spread,
slippage, and impact. It persists each
attempt as an immutable experiment, emits a ledger completion event, and verifies that
offline/full-history and online/as-of feature materialization produce the same hash. Each
backtest also persists its ordered portfolio events.

## Load real daily bars

Credentials remain only in ignored `.env`. For the Docker/PostgreSQL profile:

```sh
./scripts/compose.sh exec -T api quant-alpaca backfill AAPL \
  --start 2026-06-01T00:00:00Z \
  --end 2026-09-04T00:00:00Z \
  --timeframe 1Day
```

Daily bars use `adjustment=raw`. A bar's `available_from` is the exact XNYS session close,
including scheduled early closes; a non-session date is rejected. Raw storage preserves the
provider record while point-in-time features apply only corporate actions that were both
known and effective at the requested `as_of`.

With `APP_ENV=development`, market and news backfills are limited to
`DEVELOPMENT_MAX_BACKFILL_DAYS` (120 by default), while one-minute bars are limited to
`DEVELOPMENT_MAX_INTRADAY_BACKFILL_DAYS` (7 by default). This is intentional: local work
proves correctness with bounded samples. Multi-year backfills belong to scheduled remote
jobs under `APP_ENV=production`; changing a local limit must be an explicit choice, not a
test prerequisite.

## Run a stored-data baseline

Inside the Docker/PostgreSQL profile:

```sh
./scripts/compose.sh exec -T api quant-research run AAPL \
  --strategy momentum \
  --timeframe 1Day \
  --start 2026-06-15T00:00:00Z \
  --end 2026-09-04T00:00:00Z
```

Supported baselines are `buy_and_hold`, `momentum`, and `mean_reversion`. Optional flags
control initial equity, per-share and minimum commission, per-side slippage, half-spread,
fixed market impact, and maximum bar-volume participation. Every effective value is stored
with the experiment and every fill event records the spread assumption.

Validate stored bars independently, or import a reviewed point-in-time reference batch:

```sh
quant-research quality AAPL --timeframe 1Day --end 2026-09-04T00:00:00Z
quant-research import-reference ./reviewed-reference-batch.json
```

Reference JSON declares `dataset_type` (`corporate_actions` or `universe_memberships`),
`source`, `source_version`, and `records`. Every record must carry timezone-aware effective
and availability timestamps. The content hash and import counts are immutable and replay is
idempotent.

List recent experiments:

```sh
./scripts/compose.sh exec -T api quant-research list --limit 20
curl -fsS 'http://127.0.0.1:8000/v1/research/experiments?limit=20'
```

Inspect the ordered portfolio ledger for a run:

```sh
curl -fsS \
  'http://127.0.0.1:8000/v1/research/experiments/EXPERIMENT_ID/events'
```

Audit offline/online feature parity at an exact point in time:

```sh
./scripts/compose.sh exec -T api quant-research parity AAPL \
  --timeframe 1Day \
  --as-of 2026-09-03T20:00:00Z
```

The check materializes the feature set once from complete stored history and once from an
online-equivalent as-of slice, persists both hashes, and exits nonzero on a mismatch.

## Walk-forward validation

Run the bounded deterministic validation smoke:

```sh
make validation-smoke
```

Run validation on stored bars:

```sh
./scripts/compose.sh exec -T api quant-research validate AAPL \
  --timeframe 1Day \
  --start 2026-05-01T00:00:00Z \
  --end 2026-09-03T20:00:00Z \
  --train-bars 40 \
  --test-bars 10 \
  --step-bars 10 \
  --embargo-bars 1
```

Each fold evaluates every requested strategy on the training window, selects one using the
declared metric, and then retains every candidate's out-of-sample run. Test windows cannot
overlap and at least one bar is embargoed between train and test. The report includes
compounded and mean selected out-of-sample return, mean out-of-sample Sharpe, worst drawdown,
train-to-test Sharpe degradation, selected-strategy out-of-sample rank, a below-median
selection rate, strategy switches, and realized up/down/sideways regime summaries. Phase 3D
also resamples candidate selection across combinations of the already purged, non-overlapping
OOS folds to estimate PBO, and calculates Deflated Sharpe from those OOS fold returns.

The versioned thresholds live in `configs/research_promotion_policy.yaml`. The strict
assessment can only return `INSUFFICIENT_EVIDENCE`, `REJECTED`, or
`ELIGIBLE_FOR_HUMAN_REVIEW`; it never promotes automatically. The report separately records
Candidate Shadow eligibility, which is an observation-only deterministic gate rather than a
statistical qualification. The bounded smoke sample is intentionally too small for either.

`research_gate@0.4.0` distinguishes two subjects. An `adaptive_selector` must satisfy the
configured candidate breadth and PBO threshold. A frozen `static_strategy` has no within-
report selection contest, so candidate count and PBO are explicitly N/A; it must still pass
the total-fold, active-fold, trade-count, regime, drawdown, positive-active-OOS, and Deflated
Sharpe requirements. No-trade folds remain counted and displayed but are not treated as
losing folds. Deflated Sharpe uses the recorded search-trial count for the same
symbol/timeframe/holding-horizon family, including rejected and failed hybrid attempts.
Candidate Shadow additionally requires an exact static spec, horizon-scaled minimum activity,
positive cost-adjusted compounded OOS return, and bounded drawdown; it does not satisfy the
strict gate and cannot reach Paper.

This is a bias-detection baseline, not a claim that the selected strategy generalizes.
Inspect reports with:

```sh
curl -fsS 'http://127.0.0.1:8000/v1/research/validations?limit=20'
curl -fsS \
  'http://127.0.0.1:8000/v1/research/validations/VALIDATION_REPORT_ID'
```

## Point-in-time invariants

- `event_time <= as_of` and `available_from <= as_of` for every evidence reference.
- The snapshot records its maximum source availability time and rejects a later value.
- A completed bar may produce a signal only after its `available_from` timestamp.
- Entry may occur no earlier than the next bar open. One-bar strategies retain their bracket
  from the decision bar but recalculate reward/risk and quantity against the actual open;
  gaps that violate policy do not enter.
- Daily entry and exit records use the exchange session open and close, not the provider's
  midnight bar label.
- Every signal, submitted order, fill, portfolio mark, split, and cash dividend is stored in
  sequence with the resulting cash and position quantity.
- Split events change share quantity without creating P&L. Gross cash dividends are credited
  to held shares; dividend taxes and withholding are not modeled.
- Entry size cannot exceed the configured fraction of bar volume. An exit that cannot fully
  complete under the same cap fails closed rather than assuming impossible liquidity.
- Feature and dataset hashes include the exact source identities, timestamps, and values.
- Daily availability follows the configured exchange calendar, not a fixed UTC offset.
- Corporate actions require separate `effective_at` and `available_from` timestamps.
- Historical universes require effective intervals and an availability timestamp; selecting
  constituents uses only memberships knowable at `as_of`.
- Offline and online-equivalent materialization must hash identically for the same `as_of`.
- Strategy name/version content is immutable; changed code produces a new version.
- Every run is retained. Re-running does not overwrite an earlier result.
- Walk-forward selection uses only the training fold; every candidate's train and test
  experiment IDs remain linked from the immutable validation fold.
- Test windows do not overlap, and an explicit embargo separates each train/test pair.

## Current limitations

- SEC filing/company-fact ingestion is connected to the production coordinator. Corporate-
  action provider ingestion and point-in-time universe data acquisition are not yet
  connected; the immutable schema/store contracts and bounded fixtures are implemented.
- A provider-observed listing or post-suspension boundary is carried through every exact
  validation fold and its underlying backtest. Older rows remain queryable for audit but are
  excluded consistently from quality checks, feature warm-up, and the validation input hash.
- Split and gross cash-dividend accounting are implemented. Symbol changes still fail closed
  until cross-symbol market-data replay is supported; late split metadata is rejected because
  it would contaminate the feature path.
- The current fill model uses market orders against bar open/close with fixed slippage and
  market-impact assumptions. Multi-bar partial fills, queue position, bid/ask spread,
  cancellations, and intrabar path simulation remain open.
- Delisted-security acquisition, borrow, options fills, taxes, dynamic market impact, and
  capacity analysis are not implemented.
- The LLM research and predictive ML tools are connected through the persistent coordinator,
  but paid stages default off. Generated strategies remain research-only until exact
  validation and, when configured, deterministic automatic Shadow admission. Paper remains a
  separate administrator-confirmed boundary.
- Outcome feedback sent back into later Research LLM cycles is a bounded, schema-versioned
  summary of the newest backtests, validation gates, Shadow events, and Paper events. It does
  not embed complete fold histories in the prompt; the underlying records remain inspectable
  by ID in the Control Center.
- Production-scale statistical acceptance remains open. Local CPCV/PBO/DSR output validates
  computation and fail-closed behavior, not alpha or eligibility for deployment.
