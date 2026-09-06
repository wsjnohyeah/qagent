#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$project_root"

if [ ! -f .env.production ]; then
  echo "Refusing deployment: .env.production is missing. See docs/DEPLOYMENT.md."
  exit 1
fi
if ! grep -Eq '^TRADING_MODE=(shadow|paper)$' .env.production; then
  echo "Refusing deployment: TRADING_MODE must be shadow or paper."
  exit 1
fi
if ! grep -Eq '^LIVE_TRADING_ENABLED=false$' .env.production; then
  echo "Refusing deployment: LIVE_TRADING_ENABLED must be false."
  exit 1
fi
if ! grep -Eq '^AUTH_REQUIRED=true$' .env.production; then
  echo "Refusing deployment: AUTH_REQUIRED must be true."
  exit 1
fi
if ! grep -Eq '^ADMIN_USERNAME=.+$' .env.production; then
  echo "Refusing deployment: ADMIN_USERNAME is missing."
  exit 1
fi
if ! grep -Eq '^ADMIN_PASSWORD_HASH=.+$' .env.production; then
  echo "Refusing deployment: ADMIN_PASSWORD_HASH is missing."
  exit 1
fi
if grep -Eq '^ADMIN_PASSWORD=.+$' .env.production; then
  echo "Refusing deployment: plaintext ADMIN_PASSWORD is prohibited."
  exit 1
fi
if ! awk -F= '/^SESSION_SECRET=/{if (length($2) >= 32) ok=1} END{exit !ok}' \
  .env.production; then
  echo "Refusing deployment: SESSION_SECRET must contain at least 32 characters."
  exit 1
fi

docker compose --env-file .env.production -f compose.production.yml pull
docker compose --env-file .env.production -f compose.production.yml up -d postgres redis

attempt=0
until docker compose --env-file .env.production -f compose.production.yml exec -T postgres \
  sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    echo "PostgreSQL did not become ready for migration."
    exit 1
  fi
  sleep 2
done

docker compose --env-file .env.production -f compose.production.yml run --rm --no-deps api \
  alembic upgrade head
docker compose --env-file .env.production -f compose.production.yml up -d --remove-orphans
docker compose --env-file .env.production -f compose.production.yml ps

attempt=0
until curl -fsS http://127.0.0.1:8000/health/ready >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    docker compose --env-file .env.production -f compose.production.yml logs --tail=200 api
    exit 1
  fi
  sleep 2
done

attempt=0
until docker compose --env-file .env.production -f compose.production.yml exec -T worker \
  python -m agentic_quant.worker --healthcheck >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    docker compose --env-file .env.production -f compose.production.yml logs --tail=200 worker
    exit 1
  fi
  sleep 2
done
echo "API and worker health gates passed. New exposure remains paused."
