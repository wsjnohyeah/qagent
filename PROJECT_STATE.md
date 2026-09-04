# Project state

## Current

- Phase 0 safety scaffold is complete; Phase 1 market-data work is in progress.
- Local-lite uses Python 3.12, a project-local `uv`, SQLite, and filesystem object storage.
- The Control API, minimal web console, append-only event ledger, deterministic risk engine, and synthetic vertical slice exist.
- `live` is not a valid trading mode; `LIVE_TRADING_ENABLED=true` fails configuration validation.
- Local lint, strict type checking, 14 tests, API readiness, the HTTP vertical slice, and the secret scan pass.
- Docker Desktop 4.89.0 / Engine 29.7.2 is installed on the current Apple Silicon Mac.
- The full Compose stack is healthy: PostgreSQL 17, Redis 8, MinIO, and the API all passed direct checks; the PostgreSQL-backed shadow slice recorded six lineage events.
- `context.md` is the required master record for architecture, discussions, iterations, commit contents, and post-commit global state.
- Alembic migrations now own the ledger and normalized market-data schema.
- The read-only Alpaca adapter supports SIP historical one-minute bars, OPRA option-chain snapshots, and SIP WebSocket authentication/stream parsing.
- A real AAPL backfill stored 391 unique minute bars; replay inserted zero duplicates. A bounded OPRA request stored 10 unique option snapshots; replay inserted zero duplicates.
- Raw Alpaca responses are content-addressed in MinIO, normalized rows are stored in PostgreSQL, and new-record events are published to Redis Streams.
- The live collector has bounded reconnects and XNYS-calendar-aware intraday gap detection with automatic REST repair.

## Next

1. Capture and validate real SIP trade/quote/bar frames during an open market session.
2. Exercise disconnect/reconnect and automatic gap repair against a live session.
3. Add Redis consumer groups, durable offsets, a transactional outbox, and dead-letter replay.
4. Add data-quality reconciliation and provider lag metrics.
5. Add authentication/authorization before any production Control API exposure.
6. Expand the Decision Inspector into the full frontend.

## Blocked

- Cloud deployment needs the user's GitHub repository, VPS/provider, domain/TLS plan, and secret delivery mechanism.
- Paper submission remains blocked until the inherited percentage and dollar risk limits are reconciled.
- Real-time frame persistence cannot be externally verified until an open U.S. market session, although WebSocket authentication and synthetic frame persistence pass.

## Decisions

- ADR 0001: Redis Streams for the MVP event bus.
- ADR 0002: PostgreSQL for full environments; SQLite only for local-lite.
- ADR 0003: a no-build Control Center page for Phase 0.
- ADR 0004: Alpaca first, pending entitlement verification.
