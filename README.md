# Agentic Quant Trading System

A safety-first foundation for a cloud-hosted quantitative research, shadow-trading, and paper-trading platform. The repository contains the completed **Phase 0** safety skeleton, the read-only **Phase 1** market-data foundation, the completed **Phase 2** event/document pipeline, a **Phase 3D** bias-aware research validation gate, a **Phase 5A** reliable research-workflow layer, and a front-loaded **Phase 4B** dual-provider LLM gateway and local Control Center.

> Live-money execution is not implemented. `live` is not a valid mode, and setting `LIVE_TRADING_ENABLED=true` makes startup fail.

Before changing the project, read `context.md`. It is the master project memory containing the current global state, architecture, decisions, iteration history, and per-commit ledger. Every material change and every commit must update it using the maintenance protocol defined there.

## Start here

The current Mac does not need Docker for the first health check. The local-lite profile uses Python 3.12, SQLite, and local object storage.

```sh
make bootstrap
make check
make doctor
make run
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). The web page can run one synthetic decision and inspect the safe shadow result. API documentation is at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

`make bootstrap` installs `uv` inside `work/tools`, creates `.env` from safe defaults if needed, and resolves the locked Python environment. It does not modify the system Python.

## What exists now

The vertical slice is deliberately small but real:

```text
synthetic catalyst
  -> immutable feature snapshot
  -> strategy candidate
  -> deterministic risk decision
  -> approved trade plan
  -> shadow-only order record
  -> queryable decision lineage
```

The risk engine currently enforces mode, global pause, effective-dated restricted symbols, account floor, daily and concurrent risk, relative volume, price confirmation, sector compatibility, quote freshness, expiry, reward/risk, and capped position sizing.

This is infrastructure validation, not evidence that a strategy is profitable.

The Phase 2 path archives and normalizes SEC filings/company facts, approved issuer IR
feeds, and Alpaca News. It preserves document versions and source timing, resolves issuer
entities, and links near-duplicate coverage to one catalyst without using an LLM.

Phase 3A adds immutable evidence packets, research feature snapshots, strategy
specifications, experiment runs, and backtest trades. Its baseline runner enforces
next-bar execution and nonzero commission/slippage. This is research-infrastructure
validation, not a profitable-strategy claim.

Phase 3A.2 strengthens that path with exact XNYS session-close availability, immutable
corporate-action and historical-universe records, split-adjusted point-in-time features,
and a persisted offline/online feature-parity audit.

Phase 3B adds a deterministic portfolio event loop with separate signal, order, fill, mark,
split, and cash-dividend events. It records actual exchange open/close timestamps, applies
commission, slippage, fixed market impact, and volume-participation limits, and persists the
full event stream. Symbol changes and insufficient exit liquidity fail closed.

Phase 3C adds rolling chronological train/embargo/test folds. Every candidate is retained in
and out of sample; the report records the train-selected strategy, its out-of-sample rank,
return/Sharpe degradation, selection-failure rate, and performance by realized market regime.

Phase 3D adds combinatorial selection-risk diagnostics, Probability of Backtest Overfitting,
Deflated Sharpe, and a versioned research gate. The gate may only mark a result eligible for
human review; it never promotes a strategy automatically, and bounded synthetic evidence is
expected to fail its minimum-sample requirements.

Phase 5A adds fail-closed market-data checks, explicit half-spread fill cost, governed
corporate-action/universe imports, and durable resumable backfill partitions. These are
scale-independent workflow guarantees; they do not require a large local dataset.

Phase 4A adds an audited Responses API gateway for OpenAI GPT-5.6 Sol and Meta Muse Spark
1.3. Versioned workload routing assigns premium and value-tier models without giving either
provider access to broker credentials, risk authority, or order submission.

Phase 4B exposes that gateway in the local Control Center. The operator can create immutable
workload-routing revisions and chat through `Auto`, OpenAI, or Meta while preserving model,
route, token, latency, source, and configuration lineage. These paid/write controls remain
development-only until the remote interface has authentication and budget enforcement.

## Commands

| Command | Purpose |
|---|---|
| `make bootstrap` | Install the project-local toolchain and dependencies |
| `make check` | Run lint, strict type checking, and tests |
| `make doctor` | Boot the API, check readiness, and run the synthetic vertical slice |
| `make run` | Start the local Control API/UI with reload |
| `make demo` | Run the vertical slice in the terminal |
| `make research-smoke` | Run and persist three deterministic Phase 3 research baselines |
| `make validation-smoke` | Run bounded walk-forward, regime, and selection-bias checks |
| `make llm-routes` | Inspect workload-to-model routing and project credential readiness |
| `make llm-probe` | Make one bounded development connectivity call to each configured LLM |
| `make docker-up` | Start PostgreSQL, Redis, MinIO, and API when Docker is installed |
| `make docker-doctor` | Verify every container and the PostgreSQL-backed shadow slice |
| `make docker-alpaca-probe` | Verify SIP/OPRA REST access and SIP WebSocket authentication |
| `make docker-event-health` | Report Phase 2 document/entity/catalyst/fact counts |
| `make docker-down` | Stop the full local stack without deleting volumes |

## Safe configuration

`.env.example` contains non-secret development defaults. `.env` is ignored by Git. Production secrets belong only in a protected VPS/GitHub secret store.

Important controls:

```dotenv
APP_ENV=development
DEVELOPMENT_MAX_BACKFILL_DAYS=120
DEVELOPMENT_MAX_INTRADAY_BACKFILL_DAYS=7
TRADING_MODE=shadow
LIVE_TRADING_ENABLED=false
GLOBAL_NEW_EXPOSURE_PAUSED=false
LLM_OPENAI_API_KEY=
LLM_META_API_KEY=
LLM_ROUTING_PATH=./configs/model_routing.yaml
RESEARCH_PROMOTION_POLICY_PATH=./configs/research_promotion_policy.yaml
```

`APP_ENV=development` is a bounded correctness environment. Daily/news backfills longer than
`DEVELOPMENT_MAX_BACKFILL_DAYS`, or one-minute backfills longer than
`DEVELOPMENT_MAX_INTRADAY_BACKFILL_DAYS`, fail before contacting a provider. The future remote
deployment uses `APP_ENV=production` for durable services and governed long-horizon jobs;
this distinction does not relax point-in-time, safety, or audit invariants.

Production overrides `GLOBAL_NEW_EXPOSURE_PAUSED=true`. The red pause operation is distinct from liquidation; this build has no liquidation or live broker endpoint.

Risk values are versioned in `configs/risk_policy.yaml`. Restricted securities are effective-dated in `configs/restricted_securities.yaml`. Changes require tests and review.

Long-horizon market backfills use `quant-alpaca resumable-backfill`; completed date
partitions are skipped on rerun. `quant-research quality` persists a dataset audit, and
`quant-research import-reference` accepts reviewed, versioned corporate-action or historical-
universe JSON batches. See the market-data and research runbooks for their contracts.

## Full local infrastructure

With Docker Desktop, OrbStack, or another Docker-compatible runtime available:

```sh
docker compose config
make docker-up
make docker-doctor
```

The full profile runs PostgreSQL 17, Redis 8, MinIO, and the API. It has been exercised successfully on the initial Apple Silicon development Mac. Development ports bind only to loopback. Credentials in `docker-compose.yml` are intentionally local-only and must never be reused in production.

## Phase 1: read-only Alpaca data

Put credentials only in the ignored `.env` file:

```dotenv
ALPACA_API_KEY=...
ALPACA_API_SECRET=...
ALPACA_STOCK_FEED=sip
ALPACA_OPTION_FEED=opra
```

Never paste credentials into tracked files. Verify entitlements without placing orders:

```sh
make docker-up
make docker-alpaca-probe
```

Ingest historical one-minute bars:

```sh
./scripts/compose.sh exec -T api quant-alpaca backfill AAPL \
  --start 2026-09-02T13:30:00Z \
  --end 2026-09-02T20:00:00Z
```

Use `--timeframe 1Day` for a bounded daily-history backfill. Daily bars are marked
available at the exact XNYS session close, including scheduled early closes. Non-session
dates fail closed instead of being assigned an invented availability time.

Ingest one bounded page of an option-chain snapshot:

```sh
./scripts/compose.sh exec -T api quant-alpaca option-snapshot AAPL \
  --limit 100 --max-pages 1
```

Capture a bounded live SIP stream during market hours:

```sh
SYMBOLS=SPY,AAPL SECONDS=60 MAX_FRAMES=100 make docker-alpaca-stream
```

Every accepted provider response is content-addressed in MinIO, normalized into PostgreSQL with uniqueness constraints, recorded in the event ledger, and published to Redis Streams. Replaying identical historical data does not create duplicate normalized records or events. The live collector uses the official XNYS exchange calendar to detect missing minutes within a trading session and requests a bounded REST repair.

The final Phase 1B open-session capture/reconnect verification is intentionally pending
until the next U.S. market session.

## Phase 2: events and documents

Alpaca News uses the existing ignored Alpaca credentials:

```sh
./scripts/compose.sh exec -T api quant-events news AAPL --limit 10 --max-pages 1
```

SEC EDGAR requires an operator identity and monitored contact email in ignored `.env`:

```dotenv
SEC_USER_AGENT=Agentic Quant Research your-email@example.com
```

Then ingest bounded primary-source filing metadata and normalized XBRL company facts:

```sh
./scripts/compose.sh exec -T api quant-events sec-filings AAPL \
  --cik 0000320193 --forms 8-K,10-K,10-Q --limit 50
./scripts/compose.sh exec -T api quant-events sec-facts AAPL \
  --cik 0000320193 --max-facts 1000
```

Search normalized evidence with `quant-events search`, `GET /v1/documents/search`, or
`GET /v1/catalysts`. Investor-relations feeds require an explicit HTTPS hostname match.
Social aggregates are feature-flagged off until a licensed provider is selected. See
`runbooks/event_documents.md` for the full operating and source-trust policy.

## Phase 3: point-in-time research and event-driven replay

Run the isolated research health check:

```sh
make research-smoke
```

It creates deterministic synthetic daily bars in ignored local storage, materializes
point-in-time feature snapshots, and runs buy-and-hold, momentum, and mean-reversion
baselines. Every experiment records its data hash, strategy/code version, cost model,
metrics, trades, feature lineage, ordered portfolio events, and ledger event.

Audit an exact as-of timestamp for offline/online feature parity:

```sh
quant-research parity AAPL \
  --timeframe 1Day \
  --as-of 2026-09-03T20:00:00Z
```

The command persists both hashes and exits nonzero if they differ. Corporate-action and
universe-history tables currently accept governed fixtures/imports; provider ingestion is a
later data-source integration.

To run a baseline on stored real daily bars:

```sh
quant-research run AAPL \
  --strategy momentum \
  --timeframe 1Day \
  --start 2026-06-15T00:00:00Z \
  --end 2026-09-04T00:00:00Z
```

Use `GET /v1/research/experiments/{experiment_run_id}/events` to inspect the ordered
portfolio event stream. See `runbooks/research.md` and ADRs 0006–0012 for the exact
point-in-time invariants, fill/accounting assumptions, known limitations, and
research/runtime authority boundary.

Run the deterministic walk-forward health check:

```sh
make validation-smoke
```

For stored bars, use `quant-research validate`. Reports include PBO, Deflated Sharpe, and the
versioned fail-closed research gate. Fold lineage is available from
`GET /v1/research/validations` and `GET /v1/research/validations/{validation_report_id}`.

## Phase 4A/4B: configurable LLM gateway and local Control Center

`configs/model_routing.yaml` is the initial versioned routing source. Critical research,
strategy generation, and strategy critique route to `gpt-5.6-sol`; interactive explanations
and routine pipeline tasks route to `muse-spark-1.3`. Model IDs, reasoning effort, output
limits, and routes are configuration rather than application constants.

Inspect routing without making a paid request:

```sh
make llm-routes
```

After adding the project-specific provider keys to ignored `.env`, run bounded probes:

```sh
work/tools/uv run quant-llm probe --provider openai
work/tools/uv run quant-llm probe --provider meta
```

Every attempted call is stored with source Git SHA, route/prompt version, request, routing,
and input hashes, provider response ID, token usage, latency, status, and output. Input text
and instructions are not copied into the audit table. Automatic cross-provider fallback is
disabled so cost and model behavior cannot change silently. See `runbooks/llm_gateway.md`.

The web page now shows provider readiness and lets a development operator assign either
provider to each named workload. Saving creates an append-only database revision rather than
rewriting the tracked YAML baseline. Research Copilot supports `Auto` routing or an explicit
provider for a single conversation. Browser history is session-local and bounded; raw input is
hashed in the invocation audit while model output and usage are retained.

## Repository map

```text
src/agentic_quant/       API, market/event ingestion, domain, risk, ledger, demo pipeline
configs/                 versioned risk, restrictions, strategy, data manifest
tests/                   invariants and end-to-end replay tests
docs/adr/                architectural decisions
docs/DEPLOYMENT.md       exact handoff contract for a cloud deployment agent
context.md               master context, architecture, discussions, iterations, commits
infra/deploy/            guarded VPS deployment entry point
runbooks/                 market-data, event/document, research, and LLM operations
PROJECT_STATE.md         Current / Next / Blocked / Decisions
AGENTS.md                mandatory operating rules for coding/deployment agents
```

## Current API surface

- `GET /health/live`
- `GET /health/ready`
- `GET /v1/system/status`
- `GET /v1/events`
- `GET /v1/decisions/{correlation_id}`
- `POST /v1/demo/run`
- `POST /v1/demo/market-data`
- `GET /v1/data-health`
- `GET /v1/data-quality`
- `GET /v1/workflow-jobs`
- `GET /v1/documents/search`
- `GET /v1/catalysts`
- `GET /v1/research/experiments`
- `GET /v1/research/experiments/{experiment_run_id}/events`
- `GET /v1/research/validations`
- `GET /v1/research/validations/{validation_report_id}`
- `GET /v1/llm/routes`
- `PUT /v1/llm/routes` — development only; creates an immutable complete routing revision
- `GET /v1/llm/routes/history`
- `POST /v1/llm/chat` — development only; incurs a bounded provider call
- `GET /v1/llm/invocations`
- `GET /v1/llm/invocations/{invocation_id}`
- `POST /v1/llm/probe/{provider}` — development only; incurs a bounded provider call
- `POST /v1/market-data/alpaca/probe` — development only
- `POST /v1/market-data/alpaca/backfill` — development only
- `POST /v1/market-data/alpaca/option-snapshot` — development only
- `POST /v1/documents/alpaca-news/backfill` — development only
- `POST /v1/documents/sec/filings` — development only
- `POST /v1/documents/sec/company-facts` — development only
- `POST /v1/commands/pause`
- `POST /v1/commands/resume` — development + shadow only

The production control plane still needs authentication and authorization before it may be exposed.

## Workflow for coding agents

1. Read `AGENTS.md`, this README, `context.md`, `PROJECT_STATE.md`, and relevant ADRs.
2. Inspect the worktree and preserve unrelated user changes.
3. Keep work inside the current phase; do not add broker execution before its safety gates.
4. Add or update invariant and replay tests with every behavior change.
5. Run `make check`, `make doctor`, and `./scripts/check_no_secrets.sh`.
6. Update `context.md` for every material iteration and commit; update `PROJECT_STATE.md` and add an ADR when applicable.
7. Commit only source/config/docs—never runtime data or credentials.

## GitHub and cloud path

When a GitHub remote is supplied, the intended flow is:

```text
feature branch -> pull request -> CI -> merge main
-> immutable GHCR image -> VPS pull -> guarded Compose deployment
-> readiness gate -> new exposure remains paused
```

The cloud agent must follow `docs/DEPLOYMENT.md`. It must not invent missing credentials, expose PostgreSQL/Redis publicly, or bypass TLS/authentication. Cloud deployment is intentionally not attempted from this local bootstrap because no repository, VPS, domain, or secret channel has been selected yet.
