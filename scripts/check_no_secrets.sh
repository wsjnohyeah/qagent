#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"

pattern='(sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY)'

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  file_command='git ls-files --cached --others --exclude-standard -z'
else
  file_command="find . -type f -not -path './.venv/*' -not -path './work/*' -not -path './.git/*' -print0"
fi

if sh -c "$file_command" | xargs -0 grep -EnI --exclude=check_no_secrets.sh "$pattern" /dev/null 2>/dev/null; then
  echo "Potential secret detected."
  exit 1
fi
echo "secret-scan: PASS"
