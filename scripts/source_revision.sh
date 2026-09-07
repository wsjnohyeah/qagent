#!/bin/sh
set -eu

revision=$(git rev-parse --verify HEAD)
if ! git diff --quiet || ! git diff --cached --quiet || \
  [ -n "$(git ls-files --others --exclude-standard)" ]; then
  echo "$revision-dirty"
else
  echo "$revision"
fi
