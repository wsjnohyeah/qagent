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

For a larger range, use durable partitions. Re-running the same command skips completed
partitions and retries failed/interrupted work up to the configured attempt limit:

```sh
./scripts/compose.sh exec -T api quant-alpaca resumable-backfill AAPL \
  --start 2025-01-01T00:00:00Z \
  --end 2025-04-01T00:00:00Z \
  --timeframe 1Day \
  --partition-days 30
./scripts/compose.sh exec -T api quant-alpaca jobs --limit 20
```

Every completed ingestion and every backtest validates symbol/timeframe identity, timestamp
ordering and uniqueness, OHLC envelopes, availability time, and missing XNYS intervals.
Fatal findings create a `FAILED` quality report and stop downstream research. Inspect them at
`GET /v1/data-quality`; resumable partitions are visible at `GET /v1/workflow-jobs`.

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
# Use a bounded channel subset when validating a minute boundary:
SYMBOLS=SPY SECONDS=90 MAX_FRAMES=2 CHANNELS=bars make docker-alpaca-stream
```

The command authenticates to the configured feed and subscribes to the comma-separated
`CHANNELS` subset of `trades,quotes,bars` (all three by default). It archives each accepted
frame, persists normalized records, and publishes events to Redis. It exits when either
limit is reached. An XNYS-calendar-aware detector identifies missing minutes inside a
session and requests a REST backfill for the missing half-open interval.

### Phase 1B open-session evidence

Verified with SPY on SIP during the 2026-09-04 regular U.S. session:

- Entitlement probe authorized SIP historical data, OPRA snapshots, and SIP WebSocket bars.
- A bounded all-channel connection received 10 frames / 17 messages and inserted 4 trades
  plus 13 quotes.
- A bars-only connection persisted the 17:35 UTC minute bar. A later controlled reconnect
  persisted 17:37, emitted one `market.data.gap_detected.v1` event for 17:36, and invoked a
  completed REST repair that inserted the missing 17:36 bar.
- PostgreSQL contained the consecutive 17:35, 17:36, and 17:37 bars with distinct raw
  lineage; MinIO contained the underlying stream/REST objects and Redis advanced for every
  newly inserted record plus the gap event.
- A replay through the corrected `[start, end)` adapter returns only the missing interval and
  inserts zero duplicates.

This validates a controlled close/reconnect and recovery path. Consecutive connections that
fail before delivering market data exhaust a tested retry budget; the CLI also has a hard
duration limit. No order or broker endpoint exists in this path.

## Verify pipeline state

```sh
make docker-doctor
curl -fsS http://127.0.0.1:8000/v1/data-health
./scripts/compose.sh exec -T redis redis-cli XLEN market-events
```

The Control Center at `http://127.0.0.1:8000` shows the same high-level state.

## Failure behavior

- Missing credentials: command fails before a provider request.
- HTTP transport errors, 429 responses, server errors, and stream disconnects: bounded exponential retry/reconnect.
- Invalid or unauthorized entitlements: explicit failed capability; no fallback to a weaker feed.
- Duplicate historical bars, trades, quotes, or option snapshots: ignored by database uniqueness constraints and not republished.
- Redis, MinIO, or database unavailable: readiness fails; ingestion does not pretend to be healthy.
- Invalid OHLC/timing, duplicate timestamps, out-of-window rows, or missing exchange intervals: raw/normalized
  evidence remains inspectable, but the ingestion run and downstream research fail closed.
- Docker Hub timeout during a local build: retry; do not change data-provider settings.
