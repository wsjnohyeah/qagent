# Agentic Quant Trading System

A safety-first foundation for a cloud-hosted quantitative research, shadow-trading, and paper-trading platform. The repository contains implemented and locally verified foundations through the **Phase 7 broker boundary**: safety controls, read-only market data, event/document ingestion, point-in-time research, evidence-bound LLM analysis, ML/registry and constrained strategy generation, an authenticated System Steward, a persistent broker-free shadow runtime, a shared account/coordinator, and a fail-closed Alpaca Paper adapter with durable reconciliation. The adapter is not yet authorized for an external order because its separately validated execution profile and automatic position-exit lifecycle remain open. Implementation completeness is not the same as passing statistical or production-operational exit criteria; see `docs/REVIEW_REMEDIATION_C6A8020_2026-09-06.md`.

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
validation, not a profitable-strategy claim. The current correction record is
`docs/REVIEW_REMEDIATION_2026-09-06.md`.

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

`research_gate@0.2.0` separates candidate-selection evidence from exact frozen-strategy
evidence. PBO and minimum candidate breadth apply to an adaptive selector; they are explicitly
N/A for one static spec, which must still pass fold, regime, positive-OOS, drawdown, and
search-trial-adjusted Deflated Sharpe requirements.

Phase 5A adds fail-closed market-data checks, explicit half-spread fill cost, governed
corporate-action/universe imports, and durable resumable backfill partitions. These are
scale-independent workflow guarantees; they do not require a large local dataset.

Phase 4 adds an audited Responses API gateway for OpenAI GPT-5.6 Sol and Meta Muse Spark
1.3. Versioned workload routing assigns premium and value-tier models without giving either
provider access to broker credentials, risk authority, or order submission.

The evidence-bound analyst retrieves only document versions known at the requested cutoff,
requires structured research-only output and exact supporting quotations, abstains on
inadequate evidence, and records a Decision Inspector lineage graph. Atomic estimated-USD
reservations stop over-budget calls before they reach a provider. Token counts remain
diagnostic telemetry, not operator-configured limits.

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

The Phase 6 shadow runtime admits only an exact immutable strategy specification covered by
an exact execution-contract validation certificate. Each attempted exposure persists its
signal candidate, deterministic risk decision, approved plan, and virtual order/fill lineage,
including the account and evidence used by the gate. It models commission, spread, slippage,
impact, known-liquidity limits, the versioned protective-stop/target rule, cash, and realized
P&L. Actual observation, approval, and persistence times are distinct from market signal
time. A plan must be durable before its market open, and both quantity and reward/risk are
rechecked at that open; missed or late bars are not backfilled as forward trades. An active
deployment whose exact execution contract changed enters `REVALIDATION_REQUIRED` before it
can create new exposure. It contains no broker client or order-submission path. Bounded
local data validates the workflow; production can run the same partitionable contracts over
longer history.

All shadow strategy/symbol deployments are sleeves of one shared virtual master account.
Open plans atomically reserve account cash and concurrent risk, and completion releases the
reservation and settles P&L once. The default `$130` per-trade and `$780` concurrent risk are
an editable, versioned starting policy—not hard-coded claims about optimal sizing. A confirmed
risk revision invalidates prior execution certificates until exact validation is rerun.

The autonomous coordinator persists an hourly, per-symbol nine-stage DAG from full-window,
gap-repaired market data and refreshed Alpaca News through feature/ML/LLM research,
constrained generation, exact validation, and human-gated shadow readiness. It resumes
retryable groups across hour boundaries without repeating completed parents; exhausted stages
are explicit and require a confirmed one-attempt retry. Validation reuse requires the exact
execution contract, market-data/window fingerprint, current promotion policy, and current
research-search count. ML reuse separately requires the exact versioned training contract.
Later Research LLM calls receive bounded, point-in-time summaries of already-known backtest,
validation, Shadow, and Paper outcomes plus the forecast model's label, untouched-holdout
metrics, calibration, drift, and gate status. Paid LLM stages default off, and the coordinator
cannot promote, adopt, or submit orders.

An optional bounded market scanner now supplies that per-symbol DAG. Each hour it merges
Alpaca most-active and mover feeds with a reviewed 83-symbol theme catalog and the Focus
Watchlist, applies deterministic price/liquidity/restriction/benchmark filters, and keeps at
most 20 deep-research candidates. A budgeted LLM may re-rank only the deterministic top 40 at
most once every four hours; invalid output, provider failure, or exhausted budget falls back
to deterministic ranking. Scan evidence, scores, reasons, LLM comments, and coordinator
lineage are persisted. The resulting Candidate List has no execution authority: explicit
Trading Universe admission and the existing validation/adoption/Shadow gates still apply.

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
| `make release-check` | Run the complete local release gate, including Docker and schema drift |
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
PAPER_TRADING_ENABLED=false
PAPER_POLL_SECONDS=30
ALPACA_PAPER_BASE_URL=https://paper-api.alpaca.markets
COORDINATOR_DOCUMENT_LOOKBACK_DAYS=90
COORDINATOR_DOCUMENT_MAX_PAGES=10
MARKET_SCANNER_ENABLED=false
MARKET_SCANNER_LLM_ENABLED=false
MARKET_SCANNER_POLICY_PATH=./configs/market_scanner.yaml
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
in Compose. The red pause operation is distinct from liquidation. Paper reconciliation and
confirmed cancellation remain available while paused; no live-money endpoint exists.

Risk values are versioned in `configs/risk_policy.yaml`. Restricted securities are effective-dated in `configs/restricted_securities.yaml`. Changes require tests and review.

Each tactical risk evaluation also requires an explicit `RiskEvaluationContext`: catalyst
status where applicable, known restriction status, liquidity confirmation, market-data
health, macro-calendar status, the nearest major macro event, and duplicate-order state.
Unknown or unsafe facts reject deterministically. The current policy applies a 24-hour major
macro-event blackout unless a separately validated event strategy is explicitly approved.
Candidate/snapshot ID mismatches and signal or feature timestamps later than evaluation time
also reject.

Long-horizon market backfills use `quant-alpaca resumable-backfill`; completed fixed date
partitions retain their identity when a requested range is extended and are skipped on rerun.
`quant-research validate --strategy-spec-id ID ...` validates an exact generated immutable
spec instead of substituting a template. `quant-research quality` persists a dataset audit, and
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
and input hashes, provider response ID, token usage, estimated cost, latency, status, and
output. A sanitized request envelope is retained for the administrator's invocation
inspector; authentication headers and provider credentials are never included. Automatic
cross-provider fallback is disabled so cost and model behavior cannot change silently. See
`runbooks/llm_gateway.md`.

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

The constrained strategy generator can combine one point-in-time feature snapshot, a linked
ML forecast, and a completed evidence-bound LLM analysis. A separate adversarial critique is
mandatory. Accepted output is compiled into an immutable, research-only momentum or mean-
reversion specification; the model cannot emit executable code, position sizing, adoption,
or an order. The exact candidate must still be backtested, validated, and human-adopted.

## Phase 6: authenticated System Steward and shadow operations

The web application is organized around system objects rather than a fixed dashboard:

- **Overview** summarizes data, research, models, shadow state, pending actions, and current
  LLM estimated-USD spend against daily and monthly budget limits.
- **Lists** manages the governed trading universe, focus watchlist, candidate list,
  shadow-active symbols, benchmarks, and read-only restriction list with immutable revisions.
- **Market scanner** explains the discovery funnel and shows the latest ranked candidates,
  activity/liquidity metrics, deterministic reasons, optional LLM thesis/risks, and immutable
  source payloads. Candidate selection is visibly separate from Trading Universe permission.
- **Data explorer** exposes clickable dataset types, date groups, pagination, normalized rows,
  documents/facts, and collapsed bounded raw payloads with provenance.
- **Strategies** identifies deterministic baselines versus ML + LLM candidates and presents
  the complete creation chain: point-in-time inputs, ML forecast/model, cited Research LLM
  comment, generator proposal, independent critique, exact spec, validation, replay trades,
  and forward shadow results. Raw IDs and hashes are collapsed under Advanced diagnostics.
- **Shadow** explains the broker-free boundary and exposes candidate → risk → plan → fill
  lineage, virtual cash/P&L, alerts, reports, diagnostic ticks, and pause/retire controls.
- **Pipelines, Models, Audit, and Steward code work** expose worker liveness, jobs, quality
  evidence, invocation prompts/costs, and scoped code-change review state.
- **System Steward** is the default full-page conversation workspace. It renders safe
  Markdown, retains conversation history, and can carry the last inspected object as context.

No natural-language response executes an operation. A proposed action returns its exact
preview and confirmation phrase; only a separate authenticated request can claim and execute
it, once, before expiry. “Delete strategy” is implemented as immutable retirement. Code
requests create scoped sessions with no web shell; an external trusted coding worker must
produce a diff and passing test record before a separate commit approval can be granted.

See `runbooks/control_center.md`, `runbooks/shadow_runtime.md`, ADR 0017, and ADR 0018.
Coordinator operation is documented in `runbooks/autonomous_coordinator.md`; shared-account
and environment-isolation decisions are recorded in ADR 0022; dynamic discovery is recorded
in ADR 0028. Subject-aware validation and
open-price execution review are recorded in ADR 0023. The independent review of
`3b3926e` and its current disposition are recorded in
`docs/REVIEW_REMEDIATION_3B3926E_2026-09-06.md`.

## Phase 7: Alpaca Paper execution

Phase 7 is a separate broker boundary, not a renamed Shadow ledger. It mirrors only a
human-confirmed Paper enrollment attached to an active, exact-contract Shadow deployment.
Only plans created after enrollment are eligible. The worker creates a durable local intent
before network I/O, derives a stable Alpaca `client_order_id`, looks up that ID before every
retry, and records each observed broker lifecycle change.

The adapter supports long US-equity, whole-share, price-capped DAY bracket intents, broker
price increments, pinned account identity, idempotent recovery, and position-aware
reconciliation. Every POST rechecks current enrollment, plan, contract, restriction, buying
power, account floor/daily loss, and per-trade/concurrent dollar risk. An expired partial
entry has its remainder canceled, but a nonzero position stays open as
`POSITION_OPEN_REQUIRES_EXIT`; automatic session-close liquidation and complete child-order
tracking are not yet implemented.

Most importantly, the existing Shadow profile
`next_open_market_revalidated_bracket_one_bar@0.2.0` cannot authorize this broker behavior.
Paper requires `alpaca_day_limit_bracket_one_session@0.1.0`, and the current validator does
not issue that certificate. Enrollment and order submission therefore fail closed until a
matching validator and lifecycle are implemented. Read-only broker probing remains available.

Submission is off by default. Staged setup is:

1. Keep `TRADING_MODE=shadow`, confirm an eligible Strategy and Shadow deployment, and use
   the Paper page's read-only connection test.
2. Implement and pass the Paper-compatible execution validation/lifecycle milestone; do not
   substitute an existing Shadow certificate.
3. Only then confirm the exact deployment's `paper.enroll` preview. Historical plans remain
   excluded.
4. Set `TRADING_MODE=paper` and `PAPER_TRADING_ENABLED=true` only in the intended worker
   environment, restart, inspect account identity and pipeline heartbeat, then separately
   confirm the global resume action.

The broker base URL is hard-pinned to `https://paper-api.alpaca.markets`; configuration that
enables Paper against the live Alpaca host fails startup. There is no `live` trading mode and
`LIVE_TRADING_ENABLED=true` always fails. See `runbooks/paper_trading.md`, ADR 0024, and
ADR 0025.

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
- `GET /v1/data-quality/{report_id}`
- `GET /v1/workflow-jobs`
- `GET /v1/workflow-jobs/{job_id}`
- `GET /v1/coordinator/status`
- `GET /v1/market-scanner/status`
- `GET /v1/documents/search`
- `GET /v1/catalysts`
- `GET /v1/research/experiments`
- `GET /v1/research/experiments/{experiment_run_id}/events`
- `GET /v1/research/validations`
- `GET /v1/research/validations/{validation_report_id}`
- `POST /v1/research/strategy-candidates` — constrained ML + LLM research generation
- `GET /v1/llm/routes`
- `GET /v1/llm/budget`
- `PUT /v1/llm/budget` — creates a confirmation-gated immutable workload-limit revision
- `GET /v1/llm/budget/history`
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
- `POST /v1/commands/pause` and `/resume` — create confirmation-gated Shadow/Paper runtime actions
- `POST /v1/auth/login`, `GET /v1/auth/session`, `POST /v1/auth/logout`
- `GET /v1/control/summary`, `/v1/lists`, `/v1/explorer/*`, `/v1/strategies`
- `GET /v1/explorer/datasets/{provider}/{data_type}` and `/v1/explorer/market-bars`
- `GET /v1/strategies/{strategy_spec_id}`
- `GET /v1/threads/{object_type}/{object_id}` and authenticated discussion posts
- `GET|POST /v1/actions` plus `POST /v1/actions/{id}/confirm`
- `POST /v1/steward/ask` and persistent conversation history
- `GET /v1/shadow/deployments`, `/v1/shadow/events`, `/v1/shadow/runs`
- `GET /v1/shadow/account`
- `GET /v1/shadow/decisions`, `/v1/shadow/reports`, `/v1/shadow/alerts`
- `GET /v1/paper/status`, `/v1/paper/account`, `/v1/paper/positions`
- `GET /v1/paper/enrollments`, `/v1/paper/orders`, `/v1/paper/events`, `/v1/paper/runs`
- `POST /v1/paper/probe` — authenticated, read-only Alpaca Paper connectivity check
- `GET /v1/runtime/controls` and confirmation-gated pipeline controls
- `GET /v1/outbox/dead` and confirmation-gated single-event dead-letter requeue
- confirmation-gated `workflow.retry_exhausted` for one additional durable-stage attempt
- `GET /v1/code-changes` and tested-candidate intake

All non-health interaction is locked behind the single administrator session when
`AUTH_REQUIRED=true`. A remote deployment additionally requires TLS and the deployment gates
in `docs/DEPLOYMENT.md`.

## Workflow for coding agents

1. Read `AGENTS.md`, this README, `context.md`, `PROJECT_STATE.md`, and relevant ADRs.
2. Inspect the worktree and preserve unrelated user changes.
3. Preserve the Paper/live separation and never weaken confirmation, pause, risk, or endpoint gates.
4. Add or update invariant and replay tests with every behavior change.
5. Run `make check`, `make doctor`, and `./scripts/check_no_secrets.sh`.
6. Update `context.md` for every material iteration and commit; update `PROJECT_STATE.md` and add an ADR when applicable.
7. Commit only source/config/docs—never runtime data or credentials.

## GitHub and cloud path

Current delivery order: the four pre-cloud hardening tasks and the fail-closed Phase 7 Alpaca
Paper boundary are implemented and locally release-tested. A real bounded AAPL ML + Research LLM run is
documented in `docs/E2E_DEPLOYMENT_READINESS_AUDIT_2026-09-06.md`; it correctly abstained on
weak evidence and did not generate, adopt, or trade a strategy. Cloud bootstrap and a
read-only production Paper account probe may proceed; the matching Paper execution
validator/lifecycle, production-scale backfill, statistical promotion evidence, and continuous
shadow/Paper observation remain. No Paper order has been sent as
part of local build verification.

Main-branch CI publishes an immutable GHCR commit-SHA image after all checks pass. The guarded
deploy verifies that the image tag and embedded source revision agree. VPS backup creation and
non-destructive archive verification are provided by `infra/deploy/backup_vps.sh` and
`infra/deploy/verify_backup.sh`; `infra/deploy/restore_drill_vps.sh` exercises an isolated
disposable restore without touching production. Off-site retention and an executed restore
drill remain operator gates. The detailed North Star comparison is in
`docs/PRE_DEPLOY_NORTH_STAR_REVIEW_2026-09-06.md`; the later cache, recovery, and coherent
Paper reconciliation audit is dispositioned in
`docs/REVIEW_REMEDIATION_DE4C3D0_2026-09-07.md`.

The source repository is [wsjnohyeah/qagent](https://github.com/wsjnohyeah/qagent), with
local `main` tracking `origin/main`.

When a GitHub remote is supplied, the intended flow is:

```text
feature branch -> pull request -> CI -> merge main
-> immutable GHCR image -> VPS pull -> guarded Compose deployment
-> readiness gate -> new exposure remains paused
```

The cloud agent must follow `docs/DEPLOYMENT.md`. It must not invent missing credentials,
expose PostgreSQL/Redis publicly, or bypass TLS/authentication. Cloud deployment has not yet
been attempted because the VPS, domain/TLS approach, production secret channel, backup, and
monitoring choices are still unresolved. Once those inputs exist,
`infra/deploy/deploy_vps.sh` performs migration, idempotent environment/bootstrap checks,
service startup, and health gates while keeping new exposure paused.
