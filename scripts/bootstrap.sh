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
  umask 077
  cp "$project_root/.env.example" "$project_root/.env"
  admin_password=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
  session_secret=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
  sed -i.bak "s/__GENERATE_ADMIN_PASSWORD__/$admin_password/" "$project_root/.env"
  sed -i.bak "s/__GENERATE_SESSION_SECRET__/$session_secret/" "$project_root/.env"
  rm -f "$project_root/.env.bak"
  printf '%s\n' "$admin_password" > "$project_root/work/initial-admin-password.txt"
  echo "Created .env and work/initial-admin-password.txt with local admin credentials."
fi

cd "$project_root"
"$uv_bin" sync --all-groups
"$uv_bin" run alembic upgrade head
echo "Local environment is ready. Run: make check && make doctor"
