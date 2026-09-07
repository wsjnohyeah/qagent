#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$project_root"

if [ ! -f .env.production ]; then
  echo "Refusing backup: .env.production is missing."
  exit 1
fi

backup_root=${1:-/opt/agentic-quant/backups}
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
staging="$backup_root/$timestamp.partial"
destination="$backup_root/$timestamp"
mkdir -p "$staging"

docker compose --env-file .env.production -f compose.production.yml exec -T postgres \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' \
  > "$staging/postgres.dump"

docker compose --env-file .env.production -f compose.production.yml exec -T api \
  python -c 'import sys, tarfile; archive=tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz"); archive.add("/app/work/object-store", arcname="object-store"); archive.close()' \
  > "$staging/object-store.tar.gz"

app_image=$(sed -n 's/^APP_IMAGE=//p' .env.production)
image_sha=${app_image##*:}
{
  echo "created_at_utc=$timestamp"
  echo "app_image=$app_image"
  echo "source_git_sha=$image_sha"
  echo "database_format=postgres_custom"
  echo "object_store_format=tar_gzip"
  echo "redis_included=false"
} > "$staging/manifest.txt"

(cd "$staging" && sha256sum postgres.dump object-store.tar.gz manifest.txt > SHA256SUMS)
mv "$staging" "$destination"
echo "Backup created: $destination"
echo "Copy this directory to approved off-site storage before treating it as durable."
