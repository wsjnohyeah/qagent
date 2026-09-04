# Project state

## Current

- Phase 0 safety scaffold is implemented.
- Local-lite uses Python 3.12, a project-local `uv`, SQLite, and filesystem object storage.
- The Control API, minimal web console, append-only event ledger, deterministic risk engine, and synthetic vertical slice exist.
- `live` is not a valid trading mode; `LIVE_TRADING_ENABLED=true` fails configuration validation.
- Local lint, strict type checking, 7 tests, API readiness, the HTTP vertical slice, and the secret scan pass.
- Docker Desktop 4.89.0 / Engine 29.7.2 is installed on the current Apple Silicon Mac.
- The full Compose stack is healthy: PostgreSQL 17, Redis 8, MinIO, and the API all passed direct checks; the PostgreSQL-backed shadow slice recorded six lineage events.

## Next

1. Add Alembic migrations; replace Phase 0 `create_all` initialization.
2. Add authentication/authorization before any production Control API exposure.
3. Verify Alpaca entitlements and implement provider interfaces with replay fixtures.
4. Implement Redis Streams consumers, object-store raw payloads, and idempotency records.
5. Expand the Decision Inspector into the full frontend.

## Blocked

- Cloud deployment needs the user's GitHub repository, VPS/provider, domain/TLS plan, and secret delivery mechanism.
- External data adapters need provider accounts and entitlement confirmation.
- Paper submission remains blocked until the inherited percentage and dollar risk limits are reconciled.

## Decisions

- ADR 0001: Redis Streams for the MVP event bus.
- ADR 0002: PostgreSQL for full environments; SQLite only for local-lite.
- ADR 0003: a no-build Control Center page for Phase 0.
- ADR 0004: Alpaca first, pending entitlement verification.
