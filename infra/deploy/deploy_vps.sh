#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$project_root"

if [ ! -f .env.production ]; then
  echo "Refusing deployment: .env.production is missing. See docs/DEPLOYMENT.md."
  exit 1
fi
if ! grep -Eq '^APP_ENV=production$' .env.production; then
  echo "Refusing deployment: APP_ENV must be production."
  exit 1
fi
if ! grep -Eq '^AUTO_MIGRATE=false$' .env.production; then
  echo "Refusing deployment: AUTO_MIGRATE must be false."
  exit 1
fi
if ! grep -Eq '^DATABASE_URL=postgresql\+psycopg://' .env.production; then
  echo "Refusing deployment: DATABASE_URL must use PostgreSQL/psycopg."
  exit 1
fi
if ! grep -Eq '^REDIS_URL=redis://' .env.production; then
  echo "Refusing deployment: REDIS_URL is missing or unsupported."
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
if grep -Eq '^PAPER_TRADING_ENABLED=true$' .env.production; then
  if ! grep -Eq '^TRADING_MODE=paper$' .env.production; then
    echo "Refusing deployment: enabled Paper trading requires TRADING_MODE=paper."
    exit 1
  fi
  if ! grep -Eq '^ALPACA_PAPER_BASE_URL=https://paper-api\.alpaca\.markets/?$' \
    .env.production; then
    echo "Refusing deployment: Alpaca Paper endpoint is missing or not exact."
    exit 1
  fi
  if ! grep -Eq '^ALPACA_API_KEY=.+$' .env.production || \
    ! grep -Eq '^ALPACA_API_SECRET=.+$' .env.production; then
    echo "Refusing deployment: enabled Paper trading requires Alpaca credentials."
    exit 1
  fi
fi
if ! grep -Eq '^AUTH_REQUIRED=true$' .env.production; then
  echo "Refusing deployment: AUTH_REQUIRED must be true."
  exit 1
fi
if ! grep -Eq '^DEPLOYMENT_ENVIRONMENT_ID=.+$' .env.production; then
  echo "Refusing deployment: DEPLOYMENT_ENVIRONMENT_ID is missing."
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
docker compose --env-file .env.production -f compose.production.yml run --rm --no-deps api \
  python -m agentic_quant.production_bootstrap
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
if grep -Eq '^PAPER_TRADING_ENABLED=true$' .env.production; then
  attempt=0
  until docker compose --env-file .env.production -f compose.production.yml exec -T worker \
    python -m agentic_quant.worker --healthcheck --pipeline paper >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
      docker compose --env-file .env.production -f compose.production.yml logs --tail=200 worker
      exit 1
    fi
    sleep 2
  done
fi
attempt=0
until docker compose --env-file .env.production -f compose.production.yml exec -T coordinator \
  python -m agentic_quant.worker --healthcheck --pipeline coordinator >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    docker compose --env-file .env.production -f compose.production.yml logs --tail=200 coordinator
    exit 1
  fi
  sleep 2
done
echo "API, shadow worker, and coordinator health gates passed. New exposure remains paused."
