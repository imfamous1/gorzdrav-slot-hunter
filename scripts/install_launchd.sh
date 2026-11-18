#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077

if [ "$(uname -s)" != Darwin ]; then
  echo "LaunchAgent доступен только на macOS." >&2
  exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "Сначала выполни ./scripts/setup.sh" >&2
  exit 1
fi

PROJECT="$PWD"
PY="$PROJECT/.venv/bin/python"
LABEL="ru.spb.gorzdrav.hunter"
DOMAIN="gui/$(id -u)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
"$PY" -c 'from gorzdrav_hunter.config import load_config; load_config()'
mkdir -p "$PROJECT/logs" "$(dirname "$PLIST")"

"$PY" - "$PLIST" "$LABEL" "$PROJECT" "$PY" <<'PYCODE'
import plistlib
import sys
from pathlib import Path

plist, label, project, python = sys.argv[1:]
settings = {
    'Label': label,
    'ProgramArguments': [
        '/usr/bin/caffeinate', '-i', python, '-m', 'gorzdrav_hunter',
        'hunt', '--log-file', 'hunter.log',
    ],
    'WorkingDirectory': project,
    'RunAtLoad': True,
    'KeepAlive': {'SuccessfulExit': False},
    'ThrottleInterval': 60,
    'StandardOutPath': f'{project}/logs/launchd.out.log',
    'StandardErrorPath': f'{project}/logs/launchd.err.log',
}
path = Path(plist)
temporary = path.with_suffix('.tmp')
temporary.write_bytes(plistlib.dumps(settings))
temporary.replace(path)
PYCODE

if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "$DOMAIN/$LABEL"
fi
if ! bootstrap_error=$(launchctl bootstrap "$DOMAIN" "$PLIST" 2>&1); then
  # Некоторые пользовательские сессии macOS принимают загрузку через load.
  if ! launchctl load "$PLIST" || ! launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    printf '%s\n' "$bootstrap_error" >&2
    exit 1
  fi
fi
echo "Фоновый опрос запущен. Журнал — $PROJECT/logs/hunter.log"
