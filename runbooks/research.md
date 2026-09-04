# Point-in-time research runbook

Phase 3A provides an auditable research vertical slice. It does not authorize paper or live
orders, and its deterministic synthetic results are infrastructure checks rather than
evidence of alpha.

## One-command health check

```sh
make research-smoke
```

This command uses the isolated, ignored `work/research-smoke.db`, creates 100 deterministic
daily bars, builds point-in-time evidence and feature snapshots, and runs buy-and-hold,
momentum, and mean-reversion baselines with nonzero commission and slippage. It persists each
attempt as an immutable experiment, emits a ledger completion event, and verifies that
offline/full-history and online/as-of feature materialization produce the same hash.

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

Supported Phase 3A baselines are `buy_and_hold`, `momentum`, and `mean_reversion`. Optional
flags control initial equity, per-share commission, minimum commission, and per-side
slippage. Every effective value is stored with the experiment.

List recent experiments:

```sh
./scripts/compose.sh exec -T api quant-research list --limit 20
curl -fsS 'http://127.0.0.1:8000/v1/research/experiments?limit=20'
```

Audit offline/online feature parity at an exact point in time:

```sh
./scripts/compose.sh exec -T api quant-research parity AAPL \
  --timeframe 1Day \
  --as-of 2026-09-03T20:00:00Z
```

The check materializes the feature set once from complete stored history and once from an
online-equivalent as-of slice, persists both hashes, and exits nonzero on a mismatch.

## Point-in-time invariants

- `event_time <= as_of` and `available_from <= as_of` for every evidence reference.
- The snapshot records its maximum source availability time and rejects a later value.
- A completed bar may produce a signal only after its `available_from` timestamp.
- Entry may occur no earlier than the next bar open.
- Feature and dataset hashes include the exact source identities, timestamps, and values.
- Daily availability follows the configured exchange calendar, not a fixed UTC offset.
- Corporate actions require separate `effective_at` and `available_from` timestamps.
- Historical universes require effective intervals and an availability timestamp; selecting
  constituents uses only memberships knowable at `as_of`.
- Offline and online-equivalent materialization must hash identically for the same `as_of`.
- Strategy name/version content is immutable; changed code produces a new version.
- Every run is retained. Re-running does not overwrite an earlier result.

## Current limitations

- The fast Phase 3A runner is a baseline screen, not yet the final event-driven fill engine.
- Corporate-action provider ingestion and point-in-time universe data acquisition are not
  yet connected; the immutable schema/store contracts and bounded fixtures are implemented.
- Split-adjusted features are implemented. The current baseline runner rejects a window
  containing a known corporate action because it does not yet simulate share or cash
  changes. This is intentional fail-closed behavior.
- Delisted-security acquisition, borrow, options fills, taxes, market impact, capacity,
  walk-forward splits, CPCV/PBO, and Deflated Sharpe are not implemented.
- The LLM research orchestrator and predictive ML models are intentionally not connected
  until the validation surface can reject their candidates independently.
