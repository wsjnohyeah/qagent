# Agentic Quant Trading System

A safety-first foundation for a cloud-hosted quantitative research, shadow-trading, and paper-trading platform. This repository currently implements **Phase 0**: deterministic risk controls, an append-only decision ledger, a synthetic end-to-end replay, health endpoints, and a small Control Center.

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

## Commands

| Command | Purpose |
|---|---|
| `make bootstrap` | Install the project-local toolchain and dependencies |
| `make check` | Run lint, strict type checking, and tests |
| `make doctor` | Boot the API, check readiness, and run the synthetic vertical slice |
| `make run` | Start the local Control API/UI with reload |
| `make demo` | Run the vertical slice in the terminal |
| `make docker-up` | Start PostgreSQL, Redis, MinIO, and API when Docker is installed |
| `make docker-doctor` | Verify every container and the PostgreSQL-backed shadow slice |
| `make docker-down` | Stop the full local stack without deleting volumes |

## Safe configuration

`.env.example` contains non-secret development defaults. `.env` is ignored by Git. Production secrets belong only in a protected VPS/GitHub secret store.

Important controls:

```dotenv
APP_ENV=development
TRADING_MODE=shadow
LIVE_TRADING_ENABLED=false
GLOBAL_NEW_EXPOSURE_PAUSED=false
```

Production overrides `GLOBAL_NEW_EXPOSURE_PAUSED=true`. The red pause operation is distinct from liquidation; this build has no liquidation or live broker endpoint.

Risk values are versioned in `configs/risk_policy.yaml`. Restricted securities are effective-dated in `configs/restricted_securities.yaml`. Changes require tests and review.

## Full local infrastructure

With Docker Desktop, OrbStack, or another Docker-compatible runtime available:

```sh
docker compose config
make docker-up
make docker-doctor
```

The full profile runs PostgreSQL 17, Redis 8, MinIO, and the API. It has been exercised successfully on the initial Apple Silicon development Mac. Development ports bind only to loopback. Credentials in `docker-compose.yml` are intentionally local-only and must never be reused in production.

## Repository map

```text
src/agentic_quant/       API, domain contracts, risk engine, ledger, demo pipeline
configs/                 versioned risk, restrictions, strategy, data manifest
tests/                   invariants and end-to-end replay tests
docs/adr/                architectural decisions
docs/DEPLOYMENT.md       exact handoff contract for a cloud deployment agent
context.md               master context, architecture, discussions, iterations, commits
infra/deploy/            guarded VPS deployment entry point
PROJECT_STATE.md         Current / Next / Blocked / Decisions
AGENTS.md                mandatory operating rules for coding/deployment agents
```

## API surface in Phase 0

- `GET /health/live`
- `GET /health/ready`
- `GET /v1/system/status`
- `GET /v1/events`
- `GET /v1/decisions/{correlation_id}`
- `POST /v1/demo/run`
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
