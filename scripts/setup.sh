#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077

python3 -c 'import sys; sys.exit("Нужен Python 3.11 или новее.") if sys.version_info < (3, 11) else None'
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt
if [ ! -e config.json ]; then
  cp config.example.json config.json
fi

echo "Готово. Добавь цель через ./hunt.sh explore и проверь её через ./hunt.sh slots."
