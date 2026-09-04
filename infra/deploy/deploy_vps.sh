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

docker compose --env-file .env.production -f compose.production.yml pull
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
echo "Deployment health gate passed. New exposure remains paused."
