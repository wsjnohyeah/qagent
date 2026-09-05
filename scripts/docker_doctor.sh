#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"

./scripts/compose.sh ps
attempt=0
until curl -fsS http://127.0.0.1:8000/health/ready; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    echo "FAIL: container API did not become ready"
    ./scripts/compose.sh logs --tail=160 api
    exit 1
  fi
  sleep 1
done
echo
work/tools/uv run python scripts/authenticated_doctor.py \
  http://127.0.0.1:8000 --full-stack
curl -fsS http://127.0.0.1:9000/minio/health/live >/dev/null
./scripts/compose.sh exec -T postgres psql -U quant -d quant -Atc "SELECT 1" | grep -qx 1
./scripts/compose.sh exec -T redis redis-cli ping | grep -qx PONG
echo "docker-doctor: PASS"
