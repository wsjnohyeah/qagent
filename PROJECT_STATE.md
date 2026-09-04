# Project state

## Current

- Phases 0 and 2 are complete; the Phase 3D research-validation gate, Phase 5A reliable
  workflow layer, and front-loaded Phase 4B LLM gateway/Control Center are implemented.
  Phase 1B open-session validation is pending.
- Local-lite uses Python 3.12, a project-local `uv`, SQLite, and filesystem object storage.
- The Control API, local model/research web console, append-only event ledger, deterministic risk engine, and synthetic vertical slice exist.
- `live` is not a valid trading mode; `LIVE_TRADING_ENABLED=true` fails configuration validation.
- Local lint, strict type checking, 58 tests, API readiness, the HTTP vertical slice, and the secret scan pass.
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
- Immutable evidence packets, point-in-time feature snapshots, strategy specifications,
  experiment runs, backtest trades, corporate actions, historical universe membership,
  feature parity checks, and walk-forward reports are stored through Alembic revision
  `20260904_0016`.
- The Phase 3 runner provides buy-and-hold, long/cash momentum, and long/cash
  mean-reversion baselines with next-bar execution, commission, slippage, metrics, hashes,
  and append-only completion events.
- `make research-smoke` exercises the complete research path in an isolated local database.
- Daily bars become available at the exact XNYS close, including early closes; split-adjusted
  features use only actions known and effective at `as_of`.
- Offline/full-history and online/as-of feature materialization produce persisted comparison
  hashes.
- The Phase 3B portfolio engine persists signal/order/fill/mark/action events, uses exact
  exchange open/close timestamps, models splits and gross cash dividends, and applies
  commission, slippage, fixed market impact, and a volume-participation cap.
- Phase 3C persists rolling train/embargo/test folds, evaluates every candidate both in and
  out of sample, reports train-to-test degradation and selection failures, and separates
  selected out-of-sample results into up/down/sideways realized regimes.
- Phase 3D adds combinatorial selection-risk/PBO diagnostics, Deflated Sharpe, and a
  versioned minimum-sample/performance gate. It can only grant eligibility for human review;
  bounded local evidence remains rejected or insufficient and never promotes automatically.
- Phase 5A validates bar identity, chronology, OHLC, availability, and exchange intervals
  before research; models half-spread on both fill sides; imports point-in-time reference
  batches with source/version hashes; and resumes idempotent date-partitioned backfills.
- The Phase 4A gateway routes critical research to OpenAI `gpt-5.6-sol` and interactive or
  routine work to Meta `muse-spark-1.3` through versioned configuration. Calls are bounded,
  fail closed without project credentials, and retain immutable hashes, usage, latency,
  status, and output.
- Both Responses API adapters pass mocked contract tests and bounded live probes using
  project-specific credentials in ignored `.env`.
- The local web Control Center shows provider readiness, saves complete workload maps as
  immutable SQL revisions, and offers bounded chat through Auto/OpenAI/Meta. Route writes and
  paid chat calls are development-only; every call retains source/config/model/usage lineage.
- `.env` explicitly selects `APP_ENV=development`; development daily/news backfills are
  capped at 120 days and one-minute backfills at 7 days by default. The active scope is
  exposed by `/v1/system/status`.

## Next

1. Complete the Phase 4 research orchestrator and calibrated ML layer on top of the gateway.
2. Capture real SIP trade/quote/bar frames and reconnect/gap repair during the next open session.
3. Size remote long-horizon backfill concurrency and storage; continue using bounded samples for local
   correctness verification.
4. Extend fill realism with multi-bar partial fills, order cancellation, quote-derived
   rather than configured spread, and symbol-change/delisting replay.
5. Add Redis consumer groups, durable offsets, a transactional outbox, and dead-letter replay.
6. Add authentication, budgets, and session audit before remotely exposing the Control
   Center; then expand its data, strategy, and Decision Inspector views.

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
- ADR 0006: persist point-in-time research artifacts and validate with next-bar, cost-aware
  baselines before connecting LLM/ML strategy generation.
- ADR 0007: use exchange-session availability and bitemporal reference data; require
  offline/online feature parity and fail closed where accounting is unsupported.
- ADR 0008: persist an event-driven portfolio ledger and model split/dividend accounting,
  next-open/session-close fills, costs, market impact, and liquidity caps.
- ADR 0009: use non-overlapping rolling out-of-sample windows with an embargo, retain every
  candidate run, and report selection degradation and realized-regime results.
- ADR 0010: use a provider-neutral Responses API gateway with versioned workload routing,
  immutable invocation audits, bounded calls, and no automatic cross-provider fallback.
- ADR 0011: keep the YAML route map as a reviewed base, append development Control Center
  overrides as immutable revisions, and provide bounded Auto/OpenAI/Meta research chat.
- ADR 0012: calculate PBO and Deflated Sharpe on pre-purged OOS folds and apply a versioned,
  fail-closed eligibility gate that cannot promote automatically.
- ADR 0013: require persisted market-data quality checks, explicit spread cost, governed
  reference imports, and durable resumable partitions before Phase 6.
