#!/bin/sh
set -eu

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  exec docker compose "$@"
fi

desktop_compose=/Applications/Docker.app/Contents/Resources/cli-plugins/docker-compose
if [ -x "$desktop_compose" ]; then
  PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
  export PATH
  exec "$desktop_compose" "$@"
fi

echo "Docker Compose is unavailable. Install/start Docker Desktop or set up a compatible runtime." >&2
exit 127
