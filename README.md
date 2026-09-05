# Agentic Quant Trading System

A safety-first foundation for a cloud-hosted quantitative research, shadow-trading, and paper-trading platform. The repository contains the completed **Phase 0** safety skeleton, the verified read-only **Phase 1** market-data foundation, the completed **Phase 2** event/document pipeline, a **Phase 3D** bias-aware research validation gate, the completed **Phase 4** evidence-bound LLM analyst, the completed **Phase 5** ML/registry tooling, and the **Phase 6** authenticated System Steward, object explorer, and persistent shadow runtime.

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

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). On a new checkout, the initial
single-admin password is written to ignored `work/initial-admin-password.txt`; the username
is `admin`. Delete that password file after saving the credential in an approved password
manager. API documentation is also authentication-protected at
[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

`make bootstrap` installs `uv` inside `work/tools`, creates `.env` with generated local admin
credentials if needed, and resolves the locked Python environment. It does not modify the
system Python.

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

Phase 4 adds an audited Responses API gateway for OpenAI GPT-5.6 Sol and Meta Muse Spark
1.3. Versioned workload routing assigns premium and value-tier models without giving either
provider access to broker credentials, risk authority, or order submission.

The evidence-bound analyst retrieves only document versions known at the requested cutoff,
requires structured research-only output and exact citations, abstains on inadequate
evidence, and records a Decision Inspector lineage graph. Atomic token/estimated-cost budget
reservations stop over-budget calls before they reach a provider.

Phase 4B exposes that gateway in the local Control Center. The operator can create immutable
workload-routing revisions and chat through `Auto`, OpenAI, or Meta while preserving model,
route, token, latency, source, and configuration lineage. Paid research calls remain
development-scoped while the authenticated remote deployment path is being commissioned.

Phase 6 replaces the development console with a single-admin Control Center. Login uses a
long-lived, revocable server-side session and double-submit CSRF protection. The central
object explorer exposes lists, dataset coverage/raw payloads, strategies, pipeline state,
shadow deployments, activity, and discussion timelines. The System Steward is the default,
full-page workspace rather than a sidebar. Its persistent conversation history renders safe
Markdown, including tables and code blocks. The steward receives a bounded live system
snapshot, must cite exact object IDs, and may only create an allowlisted pending action. List
edits, pipeline controls, LLM routing, strategy adoption, shadow control, and code-change
sessions require a second explicit confirmation.

The Phase 6 shadow runtime processes newly available stored bars idempotently into a virtual
signal/order/fill journal with modeled commission, spread, slippage, impact, volume limits,
cash, and realized P&L. It contains no broker client or order-submission path. Bounded local
data validates the workflow; production can run the same partitionable contracts over longer
history.

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
| `make docker-alpaca-stream` | Persist a bounded SIP trade/quote/bar stream during market hours |
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
AUTH_REQUIRED=true
ADMIN_USERNAME=admin
ADMIN_PASSWORD=generated-local-value
SESSION_SECRET=generated-64-character-value
SESSION_MAX_AGE_DAYS=90
SHADOW_RUNTIME_ENABLED=true
SHADOW_POLL_SECONDS=30
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

Production requires `AUTH_REQUIRED=true`, `ADMIN_PASSWORD_HASH` instead of plaintext,
`GLOBAL_NEW_EXPOSURE_PAUSED=true`, and `AUTO_MIGRATE=false` at settings validation, not only
in Compose. The red pause operation is distinct from liquidation; this build has no
liquidation or live broker endpoint.

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
# Wait for minute boundaries without persisting high-frequency trades/quotes:
SYMBOLS=SPY SECONDS=90 MAX_FRAMES=2 CHANNELS=bars make docker-alpaca-stream
```

Every accepted provider response is content-addressed in MinIO, normalized into PostgreSQL with uniqueness constraints, recorded in the event ledger, and published to Redis Streams. Replaying identical historical data does not create duplicate normalized records or events. The live collector uses the official XNYS exchange calendar to detect missing minutes within a trading session and requests a bounded REST repair.

Phase 1B passed during the 2026-09-04 U.S. session: a bounded SPY run persisted real SIP
trades and quotes, bars-only sessions persisted minute bars, and a controlled reconnect
detected one missing minute and repaired it through half-open REST backfill without duplicating
the already-live boundary bar. See `runbooks/market_data.md` for the evidence and replay checks.

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

## Phase 4: configurable LLM gateway, analyst, and Control Center

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

The authenticated web page shows provider readiness and lets the administrator assign either
provider to each named workload. Saving first creates a confirmation request; confirmation
then appends a database revision rather than rewriting the tracked YAML baseline. System
Steward supports `Auto` routing or an explicit provider, persists its conversations, reads a
bounded current-state snapshot, and returns validated object citations. Raw LLM input is
hashed in the invocation audit while model output and usage are retained.

## Phase 5: calibrated ML ranking and registry

`configs/ml_policy.yaml` defines the feature list and deterministic sample, OOS, calibration,
and drift thresholds. The trainer builds point-in-time forward labels, compares logistic
regression with boosted decision stumps in expanding train/embargo/test folds, fits Platt
calibration on earlier OOS predictions, and evaluates it on a later OOS holdout. Artifacts are
portable JSON rather than executable pickle files.

Models remain `CANDIDATE` when bounded local evidence is insufficient. A model may become
`CHALLENGER` only after every ML gate passes and `CHAMPION` only after an explicit human
registry action. Champion means eligible for model serving, not strategy approval: every
downstream strategy still passes the separate PBO/DSR research gate. Forecasts can be cited
by the Phase 4 analyst, giving the Decision Inspector one ML + LLM lineage graph. See
`runbooks/ml.md` and ADR 0015.

## Phase 6: authenticated System Steward and shadow operations

The web application is organized around system objects rather than a fixed dashboard:

- **Overview** summarizes data, research, models, shadow state, pending actions, and current
  LLM token/estimated-cost usage against daily and monthly budget limits.
- **Lists** manages the governed trading universe, focus watchlist, candidate list,
  shadow-active symbols, benchmarks, and read-only restriction list with immutable revisions.
- **Data explorer** exposes coverage and bounded raw-object previews with provenance.
- **Strategies** links versioned specifications, experiments, validation evidence, adoption,
  and discussion.
- **Shadow** exposes deployments, virtual events, cash/P&L, runtime ticks, and pause/retire.
- **Pipelines, Models, Activity, and Code changes** expose controls and their audit state.
- **System Steward** is the default full-page conversation workspace. It renders safe
  Markdown, retains conversation history, and can carry the last inspected object as context.

No natural-language response executes an operation. A proposed action returns its exact
preview and confirmation phrase; only a separate authenticated request can claim and execute
it, once, before expiry. “Delete strategy” is implemented as immutable retirement. Code
requests create scoped sessions with no web shell; an external trusted coding worker must
produce a diff and passing test record before a separate commit approval can be granted.

See `runbooks/control_center.md`, `runbooks/shadow_runtime.md`, ADR 0017, and ADR 0018.

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
- `GET /v1/llm/budget`
- `PUT /v1/llm/routes` — development only; creates an immutable complete routing revision
- `GET /v1/llm/routes/history`
- `POST /v1/llm/chat` — development only; incurs a bounded provider call
- `GET /v1/llm/invocations`
- `GET /v1/llm/invocations/{invocation_id}`
- `POST /v1/intelligence/analyze` — development only; evidence-bound paid analysis
- `GET /v1/intelligence/analyses`
- `GET /v1/decision-inspector/{analysis_id}`
- `POST /v1/ml/train` — development only; trains bounded point-in-time candidates
- `GET /v1/ml/training-runs`
- `GET /v1/ml/models`
- `POST /v1/ml/forecast` — development only
- `GET /v1/ml/forecasts`
- `GET /v1/ml/registry-events`
- `POST /v1/ml/models/{model_id}/promote` — development only; human action required
- `POST /v1/llm/probe/{provider}` — development only; incurs a bounded provider call
- `POST /v1/market-data/alpaca/probe` — development only
- `POST /v1/market-data/alpaca/backfill` — development only
- `POST /v1/market-data/alpaca/option-snapshot` — development only
- `POST /v1/documents/alpaca-news/backfill` — development only
- `POST /v1/documents/sec/filings` — development only
- `POST /v1/documents/sec/company-facts` — development only
- `POST /v1/commands/pause` and `/resume` — create confirmation-gated runtime actions;
  resume is shadow-only
- `POST /v1/auth/login`, `GET /v1/auth/session`, `POST /v1/auth/logout`
- `GET /v1/control/summary`, `/v1/lists`, `/v1/explorer/*`, `/v1/strategies`
- `GET /v1/threads/{object_type}/{object_id}` and authenticated discussion posts
- `GET|POST /v1/actions` plus `POST /v1/actions/{id}/confirm`
- `POST /v1/steward/ask` and persistent conversation history
- `GET /v1/shadow/deployments`, `/v1/shadow/events`, `/v1/shadow/runs`
- `GET /v1/runtime/controls` and confirmation-gated pipeline controls
- `GET /v1/code-changes` and tested-candidate intake

All non-health interaction is locked behind the single administrator session when
`AUTH_REQUIRED=true`. A remote deployment additionally requires TLS and the deployment gates
in `docs/DEPLOYMENT.md`.

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
