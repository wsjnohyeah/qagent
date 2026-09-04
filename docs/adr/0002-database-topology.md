# ADR 0002: PostgreSQL in full environments, SQLite in local-lite

- Status: accepted
- Decision: PostgreSQL is the operational source of truth in Docker and production. SQLite is allowed only for a zero-infrastructure Phase 0 developer smoke test.
- Rationale: SQLite lets a new agent verify the safety slice immediately; PostgreSQL preserves the intended production model. SQLAlchemy keeps the current ledger portable.
- Constraint: SQLite results are never presented as production validation.

