# Market-data operations

This runbook covers read-only Alpaca data access. It never uses a trading or order endpoint.

## Configure credentials

Add the following only to the Git-ignored `.env` file:

```dotenv
ALPACA_API_KEY=...
ALPACA_API_SECRET=...
ALPACA_STOCK_FEED=sip
ALPACA_OPTION_FEED=opra
```

Do not paste credentials into documentation, commits, logs, issue trackers, or agent context. Rotate any credential disclosed through one of those channels.

Restart the API after changing `.env`:

```sh
./scripts/compose.sh up -d --force-recreate api
```

## Entitlement probe

```sh
make docker-alpaca-probe
```

Expected capabilities:

- `stock_historical` on `sip`: accessible.
- `option_chain_snapshot` on `opra`: accessible.
- `stock_stream` on `sip`: authenticated and subscribed.

The probe does not place, cancel, or inspect orders.

## Historical equity ingestion

Use timezone-aware UTC timestamps. Historical bars default to `adjustment=raw` so future corporate actions are not silently applied to point-in-time research.

```sh
./scripts/compose.sh exec -T api quant-alpaca backfill AAPL \
  --start 2026-09-02T13:30:00Z \
  --end 2026-09-02T20:00:00Z
```

Run the same command twice to verify idempotency. The second result should report `records_inserted: 0` unless the provider corrected the payload or returned additional timestamps.

## Option snapshot ingestion

The default is intentionally bounded to one page:

```sh
./scripts/compose.sh exec -T api quant-alpaca option-snapshot AAPL \
  --limit 100 --max-pages 1
```

`truncated: true` means Alpaca supplied a next-page token and only the requested bounded sample was ingested. Increase `--max-pages` deliberately; full chains can be large.

## Live stock stream

During a U.S. market session:

```sh
SYMBOLS=SPY,AAPL SECONDS=60 MAX_FRAMES=100 make docker-alpaca-stream
```

The command authenticates to the configured feed, subscribes to trades, quotes, and minute bars, archives each accepted frame, persists normalized records, and publishes events to Redis. It exits when either limit is reached.

Real live-frame persistence remains an explicit validation gate. Authentication alone is not evidence that frames were received.

## Verify pipeline state

```sh
make docker-doctor
curl -fsS http://127.0.0.1:8000/v1/data-health
./scripts/compose.sh exec -T redis redis-cli XLEN market-events
```

The Control Center at `http://127.0.0.1:8000` shows the same high-level state.

## Failure behavior

- Missing credentials: command fails before a provider request.
- HTTP transport errors, 429 responses, and server errors: bounded exponential retry.
- Invalid or unauthorized entitlements: explicit failed capability; no fallback to a weaker feed.
- Duplicate historical bars, trades, quotes, or option snapshots: ignored by database uniqueness constraints and not republished.
- Redis, MinIO, or database unavailable: readiness fails; ingestion does not pretend to be healthy.
- Docker Hub timeout during a local build: retry; do not change data-provider settings.
