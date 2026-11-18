"""Уведомления: терминал (всегда) + Telegram (если настроен) + macOS (опция)."""
from __future__ import annotations

import logging
import subprocess
import sys
from datetime import datetime

import requests

log = logging.getLogger(__name__)

COLORS = {
    'INFO': '\033[36m',      # cyan
    'OK': '\033[32m',        # green
    'SLOT': '\033[35;1m',    # bright magenta
    'WARN': '\033[33m',      # yellow
    'ERR': '\033[31;1m',     # bright red
}
RESET = '\033[0m'


class Notifier:
    def __init__(self, cfg: dict, verify: str | bool = True):
        tg = cfg.get('telegram', {})
        self.tg_enabled = bool(tg.get('enabled'))
        self.tg_token = (tg.get('bot_token') or '').strip()
        self.tg_chat = str(tg.get('chat_id') or '').strip()
        self.tg_api = (tg.get('api_url') or 'https://api.telegram.org').rstrip('/')
        self.desktop = bool(cfg.get('notify', {}).get('desktop_notification'))
        self.verify = verify
        self._tg_warned = False

    # ------------------------------------------------------------- terminal

    def _print(self, kind: str, text: str) -> None:
        stamp = datetime.now().strftime('%H:%M:%S')
        if sys.stdout.isatty():
            print(f'{stamp} {COLORS.get(kind, "")}[{kind}]{RESET} {text}', flush=True)
        else:
            print(f'{stamp} [{kind}] {text}', flush=True)

    def info(self, text: str) -> None:
        self._print('INFO', text)

    def success(self, text: str) -> None:
        self._print('OK', text)

    def warn(self, text: str) -> None:
        self._print('WARN', text)

    def error(self, text: str) -> None:
        self._print('ERR', text)

    def slot(self, text: str) -> None:
        self._print('SLOT', text)

    # ------------------------------------------------------------- telegram

    def telegram(self, text: str) -> bool:
        """Отправить в Telegram; при проблемах не роняем хантер."""
        if not self.tg_enabled:
            return False
        if not self.tg_token or not self.tg_chat:
            if not self._tg_warned:
                self._tg_warned = True
                self.warn('Telegram включён, но bot_token/chat_id не заполнены '
                          'в config.json — шлю только в терминал.')
            return False
        try:
            r = requests.post(
                f'{self.tg_api}/bot{self.tg_token}/sendMessage',
                json={
                    'chat_id': self.tg_chat,
                    'text': text,
                    'disable_web_page_preview': True,
                },
                timeout=15,
                verify=self.verify,
            )
            if r.status_code != 200:
                self.warn(f'Telegram ответил HTTP {r.status_code}: {r.text[:200]}')
                return False
            return True
        except requests.RequestException as e:
            self.warn(f'Telegram недоступен: {e.__class__.__name__}')
            return False

    # ------------------------------------------------------------- combo

    def broadcast(self, text: str, kind: str = 'INFO') -> None:
        self._print(kind, text)
        self.telegram(text)

    def desktop_notify(self, title: str, text: str) -> None:
        if not self.desktop or sys.platform != 'darwin':
            return
        try:
            subprocess.run(
                ['osascript', '-e', 'on run argv\n'
                 'display notification (item 2 of argv) with title (item 1 of argv) '
                 'sound name "Glass"\nend run', title[:80], text[:200]],
                check=False, timeout=10,
                capture_output=True,
            )
        except Exception as e:  # noqa: BLE001 — уведомление не должно ронять
            log.debug('desktop notify failed: %s', e)
