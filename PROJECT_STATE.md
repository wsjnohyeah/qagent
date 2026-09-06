# Project state

## Current

- Implemented foundations through the corrected Phase 6.1 baseline pass bounded-development validation, including
  Phase 1B open-session checks, corrected point-in-time research/ML contracts, constrained
  ML + LLM strategy generation, and the authenticated Control Center/System Steward/shadow
  decision lineage. Phase 5 statistical promotion and Phase 6 continuous-operation exit
  criteria have not passed because bounded development data and elapsed observation time are
  intentionally insufficient.
- Local-lite uses Python 3.12, a project-local `uv`, SQLite, and filesystem object storage.
- The Control API, single-admin object-centric web console, persistent System Steward,
  append-only event ledger, deterministic risk engine, and shadow runtime exist.
- `live` is not a valid trading mode; `LIVE_TRADING_ENABLED=true` fails configuration validation.
- Local lint, strict type checking, the full test suite, API readiness, the authenticated HTTP
  vertical slice, and the secret scan pass.
- Docker Desktop 4.89.0 / Engine 29.7.2 is installed on the current Apple Silicon Mac.
- The full Compose stack is healthy: PostgreSQL 17, Redis 8, MinIO, and the API all passed direct checks; the PostgreSQL-backed shadow slice recorded six lineage events.
- `context.md` is the required master record for architecture, discussions, iterations, commit contents, and post-commit global state.
- Alembic migrations now own the ledger and normalized market-data schema.
- The read-only Alpaca adapter supports SIP historical one-minute bars, OPRA option-chain snapshots, and SIP WebSocket authentication/stream parsing.
- A real AAPL backfill stored 391 unique minute bars; replay inserted zero duplicates. A bounded OPRA request stored 10 unique option snapshots; replay inserted zero duplicates.
- Raw Alpaca responses are content-addressed in MinIO, normalized rows are stored in PostgreSQL, and new-record events are published to Redis Streams.
- The live collector has bounded reconnects and XNYS-calendar-aware intraday gap detection with automatic REST repair.
- A real open-session SPY run persisted SIP trades, quotes, and minute bars. A controlled
  reconnect detected a missing minute, emitted its gap event, and repaired it through REST.
- Historical windows are strictly half-open even though Alpaca's REST `end` is inclusive;
  out-of-window normalized rows now fail `market_data_quality@0.2.0`.
- Phase 2 stores immutable source-document versions, issuer entities, normalized SEC XBRL facts, and deduplicated catalysts.
- Alpaca News, SEC EDGAR, approved-host IR, and disabled-by-default social aggregate adapters exist.
- A real 10-article Alpaca News page passed MinIO/PostgreSQL/Redis ingestion; replay inserted zero new records or events.
- Ten Apple Newsroom primary-source entries passed the same path; replay inserted zero new records or events.
- Twenty AAPL SEC filing records normalized into 19 deduplicated catalysts; replay inserted zero new records or events.
- A bounded set of 250 AAPL SEC XBRL company facts normalized successfully; replay inserted zero duplicates.
- Immutable evidence packets, point-in-time feature snapshots, strategy specifications,
  experiment runs, backtest trades, corporate actions, historical universe membership,
  feature parity checks, and walk-forward reports are stored through Alembic revision
  `20260906_0026`, including exact validation contracts, shadow risk lineage, fenced workflow
  attempts, generation-attempt audit, runtime leases, and the event outbox.
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
- The local web Control Center is locked behind one persistent administrator session and
  CSRF protection. Its default view is a dedicated full-page System Steward workspace with
  persistent conversations and safely rendered Markdown; the remaining navigation exposes
  overview, lists, data, strategy, shadow, pipeline, model, activity, and code-change objects.
- Overview reads the persistent LLM budget ledger and shows current daily/monthly estimated-
  USD consumption, reservations, limits, and provider/workload utilization.
- Each workload's daily estimated-USD limit is editable through an immutable, explicit-
  confirmation revision. There is no operator token ceiling. Changes preserve current-period
  spend and immediately recalculate percentage usage against the new cap; the YAML project
  daily limit remains a hard cap.
- One System Steward reads a bounded current-state snapshot, returns validated object
  citations, persists conversations, and can propose allowlisted admin actions. It cannot
  execute them; a separate exact confirmation is required.
- The persistent broker-free shadow runtime admits only the exact static strategy ID and
  execution contract covered by a gate-eligible, human-confirmed validation certificate.
  It records candidate → deterministic risk decision → approved plan → modeled virtual
  order/fill lineage, cash, and P&L. A plan is persisted on one completed bar and can execute
  only on a later bar; missed bars are never synthesized into forward fills. Research-only
  buy-and-hold cannot be shadow-adopted. No broker adapter exists.
- The global new-exposure pause is enforced inside the shadow tick boundary, so a manually
  confirmed tick cannot bypass the scheduler's kill switch.
- Tactical risk evaluation now requires explicit, auditable catalyst, restriction-status,
  liquidity, data-health, macro-calendar, and duplicate-order facts. Unknown/unsafe facts
  reject, and `risk_policy@0.3.0` applies a 24-hour major-macro-event blackout unless the
  strategy is separately approved for that event. The policy also owns the baseline
  stop/target geometry used identically by research and shadow. Candidate/snapshot mismatches
  and future signal/feature timestamps also reject.
- GitHub `origin` is `https://github.com/wsjnohyeah/qagent.git`; local and remote `main`
  were synchronized at `06b6853` before this remediation iteration.
- GitHub Actions uses the current Node 24-based `actions/checkout@v7.0.1` and
  `astral-sh/setup-uv@v10.0.1` releases.
- Current corrected baseline passes 106 tests, authenticated local and PostgreSQL/MinIO/Redis
  doctors, JavaScript parsing, schema migration checks, zero PostgreSQL schema drift, and the
  repository secret scan. Paid providers were not called during this remediation.
- Phase 4 adds point-in-time document retrieval, `research_analysis@0.2.0`, exact quotation
  validation, deterministic abstention, atomic estimated-USD reservations, and Decision
  Inspector graph `ai_infrastructure_graph@0.1.0`.
- Phase 5 builds point-in-time labels, compares logistic and boosted-stump models with
  embargoed walk-forward splits, separates purged OOS calibration, model-selection, and final
  holdout partitions, measures PSI drift, stores JSON artifacts/forecasts, and enforces human-
  only champion promotion. Labels match the executable next-open-to-future-close contract
  and advance by actual bars rather than sparse snapshot rows.
- A constrained generator combines a matching feature snapshot, linked ML forecast, and
  evidence-bound analysis, then requires adversarial LLM critique before compiling an
  immutable research-only strategy DSL. It cannot emit code, size exposure, adopt a strategy,
  or place an order.
- Bounded local ML evidence remains `CANDIDATE`; no model or strategy has been promoted.
- `.env` explicitly selects `APP_ENV=development`; development daily/news backfills are
  capped at 120 days and one-minute backfills at 7 days by default. The active scope is
  exposed by `/v1/system/status`.
- Production settings fail unless authentication is enabled with a hash-only password, new
  exposure starts paused, and automatic migration is disabled. The production local raw
  archive is mounted on a persistent named volume.
- Production Compose separates the authenticated API from a persistent shadow worker with a
  SQL heartbeat. Workflow jobs have dependency-aware leases and ownership checks; ledger
  events have a retryable SQL outbox with stable event IDs and dead-letter visibility.
- Phase 6.1 data pages support dataset-specific drill-down, date grouping, pagination,
  normalized-object views, and collapsed raw payloads. Strategy pages explain provenance and
  expose exact validation, experiments, trades, and shadow state. Pipeline jobs and quality
  reports are individually inspectable.

## Next

1. Run and document the agreed continuous shadow observation period and replay comparison.
2. Continue interactive Phase 6.1 UI review with real operator navigation and refine labels
   where the user identifies remaining ambiguity.
3. Size remote long-horizon backfill concurrency and storage; continue using bounded samples for local
   correctness verification.
4. Extend fill realism with multi-bar partial fills, order cancellation, quote-derived
   rather than configured spread, and symbol-change/delisting replay.
5. Add operator-driven dead-letter replay and dedicated collection/research worker schedules;
   the durable outbox and lease contracts are now implemented.
6. Select TLS/reverse proxy, backup, monitoring, and secret delivery before remotely exposing
   the already-authenticated Control Center.

## Blocked

- Cloud deployment needs the VPS/provider, domain/TLS plan, backup/monitoring choices, and
  secret delivery mechanism. The GitHub repository is configured.
- Paper submission remains blocked until the inherited percentage and dollar risk limits are reconciled.

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
- ADR 0014: bound every LLM call by versioned budgets and accept research analysis only when
  point-in-time evidence, structured output, and exact citations validate.
- ADR 0015: train transparent chronological ML baselines, measure calibration/drift, store
  safe JSON artifacts, and require deterministic eligibility plus a human for champion status.
- ADR 0016: verify real SIP persistence/recovery during an open session and enforce half-open
  historical windows plus production fail-closed startup/persistence invariants.
- ADR 0017: use one authenticated System Steward with object context and explicit,
  expiring, single-use confirmation for sensitive operations.
- ADR 0018: run adopted strategies in a persistent, idempotent, broker-free shadow runtime.
- ADR 0019: require explicit external risk context and enforce pause at the shadow boundary.
- ADR 0020: bind exact research/execution contracts and use recoverable Phase 6 operations.
- ADR 0021: require forward shadow timing, complete execution-contract binding, durable event
  reconciliation, and attempt-level workflow/runtime fencing.
