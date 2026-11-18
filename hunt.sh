#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
umask 077
if [ ! -x .venv/bin/python ]; then
  echo "Сначала выполни ./scripts/setup.sh" >&2
  exit 1
fi
if [ "$#" -eq 0 ]; then
  set -- hunt
fi
exec .venv/bin/python -m gorzdrav_hunter "$@"
