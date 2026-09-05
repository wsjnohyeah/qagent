#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
uv_bin="$project_root/work/tools/uv"
port=${API_PORT:-8765}
log_file="$project_root/work/doctor-api.log"

if [ ! -x "$uv_bin" ]; then
  echo "FAIL: project-local uv is missing; run make bootstrap"
  exit 1
fi

cd "$project_root"
echo "uv: $($uv_bin --version)"
echo "python: $($uv_bin run python --version)"

if command -v docker >/dev/null 2>&1; then
  echo "docker: $(docker --version)"
elif [ -x /Applications/Docker.app/Contents/Resources/bin/docker ]; then
  docker_bin=/Applications/Docker.app/Contents/Resources/bin/docker
  echo "docker: $($docker_bin --version) (Docker Desktop application path)"
else
  echo "docker: unavailable (local-lite works; full infrastructure is not runnable yet)"
fi

rm -f "$log_file"
"$uv_bin" run uvicorn agentic_quant.api:app --host 127.0.0.1 --port "$port" >"$log_file" 2>&1 &
api_pid=$!
trap 'kill "$api_pid" 2>/dev/null || true' EXIT INT TERM

attempt=0
until curl -fsS "http://127.0.0.1:$port/health/ready" >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    echo "FAIL: API did not become ready"
    sed -n '1,160p' "$log_file"
    exit 1
  fi
  sleep 1
done

curl -fsS "http://127.0.0.1:$port/health/ready"
echo
"$uv_bin" run python scripts/authenticated_doctor.py "http://127.0.0.1:$port"
echo "doctor: PASS"
