#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 /absolute/path/to/backup-directory"
  exit 1
fi

backup_dir=$1
for name in postgres.dump object-store.tar.gz manifest.txt SHA256SUMS; do
  if [ ! -f "$backup_dir/$name" ]; then
    echo "Invalid backup: missing $name"
    exit 1
  fi
done

(cd "$backup_dir" && sha256sum -c SHA256SUMS)
tar -tzf "$backup_dir/object-store.tar.gz" >/dev/null

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$project_root"
docker compose --env-file .env.production -f compose.production.yml exec -T postgres \
  pg_restore --list < "$backup_dir/postgres.dump" >/dev/null

echo "Backup structure, checksums, object archive, and PostgreSQL catalog are valid."
echo "A destructive restore drill still requires an isolated disposable database."
