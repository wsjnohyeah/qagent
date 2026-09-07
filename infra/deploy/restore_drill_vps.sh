#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 /absolute/path/to/backup-directory"
  exit 1
fi

backup_dir=$1
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
"$script_dir/verify_backup.sh" "$backup_dir"

container="agentic-quant-restore-drill-$$"
postgres_image="postgres:17-alpine@sha256:18cfe3ef5e6815560c98237d6216d1e5119702fb0f3894c8785dd58b8bbe5d73"
restore_dir=$(mktemp -d)
cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
  rm -rf "$restore_dir"
}
trap cleanup EXIT INT TERM

docker run -d --name "$container" --network none \
  -e POSTGRES_PASSWORD=restore-drill-only \
  -e POSTGRES_DB=restore_drill \
  "$postgres_image" >/dev/null

attempt=0
until docker exec "$container" pg_isready -U postgres -d restore_drill \
  >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    echo "Disposable PostgreSQL did not become ready."
    exit 1
  fi
  sleep 1
done

docker exec -i "$container" pg_restore \
  -U postgres -d restore_drill --no-owner --no-privileges \
  < "$backup_dir/postgres.dump"
schema_version=$(docker exec "$container" psql -U postgres -d restore_drill \
  -Atc 'select version_num from alembic_version')
if [ -z "$schema_version" ]; then
  echo "Restore drill failed: restored database has no Alembic version."
  exit 1
fi

tar -xzf "$backup_dir/object-store.tar.gz" -C "$restore_dir"
if [ ! -d "$restore_dir/object-store" ]; then
  echo "Restore drill failed: object-store root was not restored."
  exit 1
fi

echo "Isolated restore drill passed at schema $schema_version."
echo "The disposable database container and extracted files will now be removed."
