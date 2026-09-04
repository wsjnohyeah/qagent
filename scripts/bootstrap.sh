#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
uv_bin="$project_root/work/tools/uv"

mkdir -p "$project_root/work/tools" "$project_root/work/object-store"

if [ ! -x "$uv_bin" ]; then
  echo "Installing project-local uv..."
  curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR="$project_root/work/tools" sh
fi

if [ ! -f "$project_root/.env" ]; then
  cp "$project_root/.env.example" "$project_root/.env"
  echo "Created .env from safe local defaults."
fi

cd "$project_root"
"$uv_bin" sync --all-groups
echo "Local environment is ready. Run: make check && make doctor"

