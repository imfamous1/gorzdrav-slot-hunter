#!/bin/bash
set -euo pipefail

if [ "$(uname -s)" != Darwin ]; then
  echo "LaunchAgent доступен только на macOS." >&2
  exit 1
fi
LABEL="ru.spb.gorzdrav.hunter"
SERVICE="gui/$(id -u)/$LABEL"
if launchctl print "$SERVICE" >/dev/null 2>&1; then
  launchctl bootout "$SERVICE"
fi
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Фоновый опрос выключен."
