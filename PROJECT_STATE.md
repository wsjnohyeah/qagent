# Project state

## Current

- Phases 0 and 2 are complete; Phase 1B open-session validation is pending until the next U.S. market session.
- Local-lite uses Python 3.12, a project-local `uv`, SQLite, and filesystem object storage.
- The Control API, minimal web console, append-only event ledger, deterministic risk engine, and synthetic vertical slice exist.
- `live` is not a valid trading mode; `LIVE_TRADING_ENABLED=true` fails configuration validation.
- Local lint, strict type checking, 22 tests, API readiness, the HTTP vertical slice, and the secret scan pass.
- Docker Desktop 4.89.0 / Engine 29.7.2 is installed on the current Apple Silicon Mac.
- The full Compose stack is healthy: PostgreSQL 17, Redis 8, MinIO, and the API all passed direct checks; the PostgreSQL-backed shadow slice recorded six lineage events.
- `context.md` is the required master record for architecture, discussions, iterations, commit contents, and post-commit global state.
- Alembic migrations now own the ledger and normalized market-data schema.
- The read-only Alpaca adapter supports SIP historical one-minute bars, OPRA option-chain snapshots, and SIP WebSocket authentication/stream parsing.
- A real AAPL backfill stored 391 unique minute bars; replay inserted zero duplicates. A bounded OPRA request stored 10 unique option snapshots; replay inserted zero duplicates.
- Raw Alpaca responses are content-addressed in MinIO, normalized rows are stored in PostgreSQL, and new-record events are published to Redis Streams.
- The live collector has bounded reconnects and XNYS-calendar-aware intraday gap detection with automatic REST repair.
- Phase 2 stores immutable source-document versions, issuer entities, normalized SEC XBRL facts, and deduplicated catalysts.
- Alpaca News, SEC EDGAR, approved-host IR, and disabled-by-default social aggregate adapters exist.
- A real 10-article Alpaca News page passed MinIO/PostgreSQL/Redis ingestion; replay inserted zero new records or events.
- Ten Apple Newsroom primary-source entries passed the same path; replay inserted zero new records or events.
- Twenty AAPL SEC filing records normalized into 19 deduplicated catalysts; replay inserted zero new records or events.
- A bounded set of 250 AAPL SEC XBRL company facts normalized successfully; replay inserted zero duplicates.

## Next

1. Capture real SIP trade/quote/bar frames and reconnect/gap repair during the next open session.
2. Begin Phase 3 with the point-in-time feature registry and offline/online parity tests.
3. Add Redis consumer groups, durable offsets, a transactional outbox, and dead-letter replay.
4. Add data-quality reconciliation and provider lag metrics.
5. Build a labeled corpus to measure cross-provider catalyst dedup precision/recall.
6. Add authentication/authorization before any production Control API exposure.
7. Expand the Decision Inspector into the full frontend.

## Blocked

- Cloud deployment needs the user's GitHub repository, VPS/provider, domain/TLS plan, and secret delivery mechanism.
- Paper submission remains blocked until the inherited percentage and dollar risk limits are reconciled.
- Real-time frame persistence cannot be externally verified until an open U.S. market session, although WebSocket authentication and synthetic frame persistence pass.

## Decisions

- ADR 0001: Redis Streams for the MVP event bus.
- ADR 0002: PostgreSQL for full environments; SQLite only for local-lite.
- ADR 0003: a no-build Control Center page for Phase 0.
- ADR 0004: Alpaca first, pending entitlement verification.
- ADR 0005: version source evidence and deduplicate catalysts deterministically.
