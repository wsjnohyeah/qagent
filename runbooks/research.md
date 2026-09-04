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
attempt as an immutable experiment and emits a ledger completion event.

## Load real daily bars

Credentials remain only in ignored `.env`. For the Docker/PostgreSQL profile:

```sh
./scripts/compose.sh exec -T api quant-alpaca backfill AAPL \
  --start 2026-06-01T00:00:00Z \
  --end 2026-09-04T00:00:00Z \
  --timeframe 1Day
```

Daily bars use `adjustment=raw`. A bar's `available_from` is conservatively set to the next
UTC day so the completed daily bar cannot enter a same-day decision. Corporate-action-aware
research remains a separate Phase 3 requirement.

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

## Point-in-time invariants

- `event_time <= as_of` and `available_from <= as_of` for every evidence reference.
- The snapshot records its maximum source availability time and rejects a later value.
- A completed bar may produce a signal only after its `available_from` timestamp.
- Entry may occur no earlier than the next bar open.
- Feature and dataset hashes include the exact source identities, timestamps, and values.
- Strategy name/version content is immutable; changed code produces a new version.
- Every run is retained. Re-running does not overwrite an earlier result.

## Current limitations

- The fast Phase 3A runner is a baseline screen, not yet the final event-driven fill engine.
- Daily bar availability is deliberately conservative rather than exchange-close exact.
- Corporate actions, delisted-universe membership, borrow, options fills, taxes, market
  impact, capacity, walk-forward splits, CPCV/PBO, and Deflated Sharpe are not implemented.
- The LLM research orchestrator and predictive ML models are intentionally not connected
  until the validation surface can reject their candidates independently.
