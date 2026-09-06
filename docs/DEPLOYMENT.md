# Agentic deployment runbook

This document is the handoff contract for an agent deploying the repository after local validation and a GitHub push. It does not authorize live trading.

## Hard deployment gates

Do not deploy until all of the following are true:

- The Git worktree is clean and the target commit is known.
- GitHub CI passed on that exact commit.
- The image is stored in GHCR under an immutable commit-SHA tag.
- A Linux VPS with Docker Engine and Compose v2 exists.
- DNS and TLS termination have been selected.
- Production secrets were delivered through an approved secret mechanism.
- `TRADING_MODE` is `shadow` or `paper`.
- `LIVE_TRADING_ENABLED=false`.
- Paper-broker credentials, if present, are verified unable to access a live account.
- Phase 6 single-admin authentication is enabled with a production Argon2 hash.
- The Control API is not public until TLS termination is configured and verified.

If any gate is missing, stop and report the exact missing item. Do not improvise around it.

## Required operator inputs

The deployment agent needs:

1. GitHub repository URL and immutable commit/tag.
2. GHCR image name, such as `ghcr.io/OWNER/REPO:COMMIT_SHA`.
3. VPS SSH target and a non-root deploy account with Docker access.
4. Domain name and reverse-proxy/TLS decision.
5. Secret-delivery method; never request secrets in chat or commit them.
6. `shadow` versus `paper`; default to `shadow` when not explicitly approved.

## Production environment file

Create `/opt/agentic-quant/.env.production` on the VPS with permissions `0600`. Minimum shape:

```dotenv
APP_IMAGE=ghcr.io/OWNER/REPO:COMMIT_SHA
APP_ENV=production
TRADING_MODE=shadow
LIVE_TRADING_ENABLED=false
GLOBAL_NEW_EXPOSURE_PAUSED=true
AUTH_REQUIRED=true
ADMIN_USERNAME=admin
ADMIN_PASSWORD_HASH=ARGON2ID_HASH_FROM_APPROVED_SECRET_STORE
SESSION_SECRET=AT_LEAST_32_RANDOM_CHARACTERS
SESSION_MAX_AGE_DAYS=90
SHADOW_RUNTIME_ENABLED=true
SHADOW_POLL_SECONDS=30
AUTO_MIGRATE=false
POSTGRES_DB=quant
POSTGRES_USER=quant
POSTGRES_PASSWORD=GENERATE_IN_SECRET_STORE
DATABASE_URL=postgresql+psycopg://quant:URL_ENCODED_PASSWORD@postgres:5432/quant
REDIS_URL=redis://redis:6379/0
OBJECT_STORE_ROOT=/app/work/object-store
```

The production Compose file mounts `/app/work/object-store` on the named
`object-store-data` volume so raw evidence survives API replacement. Include that volume in
backups, or deliberately configure an external S3-compatible object store before deployment.

`APP_ENV=production` selects the durable, long-horizon operating profile. The development
backfill cap does not apply, but long jobs must run through authenticated, observable worker
operations rather than unauthenticated public API endpoints.

Provider keys are added only when their integration phase is approved. Redact them from logs and health responses.

Generate the production hash interactively without putting the password in shell history:

```sh
work/tools/uv run python -c \
  'from getpass import getpass; from argon2 import PasswordHasher; print(PasswordHasher().hash(getpass()))'
```

Store only the resulting hash as `ADMIN_PASSWORD_HASH`; generate `SESSION_SECRET` with the
approved secret manager. Do not set `ADMIN_PASSWORD` in production.

## Build and publish

From the clean, reviewed commit:

```sh
make check
make doctor
./scripts/check_no_secrets.sh
docker build --build-arg SOURCE_GIT_SHA=$(git rev-parse HEAD) \
  -t ghcr.io/OWNER/REPO:COMMIT_SHA .
docker push ghcr.io/OWNER/REPO:COMMIT_SHA
```

Never publish a mutable `latest` tag as the only rollback reference.

Before building the image, `make release-check` provides the combined local gate: lint,
strict types, full tests, local-lite doctor, secret scan, Compose rebuild/doctor, and
PostgreSQL Alembic drift detection.

## First VPS deployment

Place the checked-out repository at `/opt/agentic-quant`, install `.env.production`, set `APP_IMAGE` in the shell or environment file, and run:

```sh
cd /opt/agentic-quant
./infra/deploy/deploy_vps.sh
curl -fsS http://127.0.0.1:8000/health/ready
```

The API is loopback-only. Add a TLS reverse proxy only after verifying login, session-cookie,
CSRF, and logout behavior through that proxy. Do not expose ports 5432 or 6379.

The guarded deploy script starts PostgreSQL, waits for readiness, applies Alembic migrations
as a one-shot task, and only then replaces the API and dedicated shadow worker. The API does
not own the production scheduler. Deployment fails if either API readiness or the worker's
persistent heartbeat is unhealthy. `AUTO_MIGRATE` remains false in both long-running
services.

## Post-deploy verification

Verify and record:

- Exact Git commit and image digest.
- `/health/live` and `/health/ready` responses.
- `APP_ENV=production`, `TRADING_MODE=shadow|paper`, live disabled, exposure paused.
- PostgreSQL and Redis health.
- Restart behavior after one controlled API restart.
- Backup output and a restore test before durable operation.
- No credentials appear in container logs.
- An unauthenticated request to `/v1/system/status` returns `401` while health probes remain
  available.
- Login succeeds through TLS, mutating requests reject a missing CSRF header, logout revokes
  the session, and the administrator credential is not plaintext in the environment file.

Phase 6 supplies application authentication, but a public production deployment still needs
TLS, backups, monitoring, secret delivery, and infrastructure access controls. Until those
gates pass, deployment may only be an isolated engineering preview.

## Rollback

Keep the prior immutable image tag. To roll back, set `APP_IMAGE` to that prior tag and rerun `infra/deploy/deploy_vps.sh`. Never roll back across an incompatible database migration without its documented restore/migration procedure.

Rollback does not delete volumes. Destructive database recovery requires an explicit operator decision and a verified backup.
