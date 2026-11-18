"""Управление фоновым поиском и журнал настольного приложения."""
from __future__ import annotations

from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler
import os
import subprocess
import sys
import time
from uuid import uuid4

from .booker import Booker
from .client import GorzdravClient
from .config import (
    CONFIG_PATH, LOG_DIR, ROOT, _read_json, _write_json, load_config,
    load_profile, load_state, save_profile, save_state,
)
from .hunter import Hunter
from .notify import Notifier
from .paths import RESOURCES
from .process_lock import is_locked, process_lock
from .tls import build_bundle

log = logging.getLogger(__name__)
STATUS = ROOT / 'search.json'
STOP = ROOT / 'stop.json'
EVENTS = ROOT / 'events.json'
CONTROL_LOCK = ROOT / '.cache' / 'desktop.lock'
WORKER_LOCK = ROOT / '.cache' / 'hunter.lock'


def read_status() -> dict:
    status = _read_json(STATUS, default={})
    if is_locked(WORKER_LOCK):
        phase = 'stopping' if _stop_requested(status.get('id')) else 'running'
    elif status.get('phase') == 'starting' and time.time() - status['started_at'] < 30:
        phase = 'stopping' if _stop_requested(status['id']) else 'starting'
    else:
        phase = status.get('phase', 'idle')
        if phase in ('starting', 'running'):
            phase = 'interrupted'
    return {**status, 'phase': phase}


def _stop_requested(run_id: str | None) -> bool:
    return bool(run_id) and _read_json(STOP, default={}).get('id') == run_id


def request_stop() -> None:
    with process_lock(CONTROL_LOCK):
        status = read_status()
        if status['phase'] in ('starting', 'running'):
            _write_json(STOP, {'id': status['id']})


def start_search(config: dict, profile: dict) -> None:
    with process_lock(CONTROL_LOCK):
        if read_status()['phase'] in ('starting', 'running', 'stopping'):
            raise ValueError('Поиск уже запущен.')
        _write_json(CONFIG_PATH, config)
        save_profile(profile)
        # Каждый явно запущенный поиск повторно рассматривает актуальные талоны.
        state = load_state()
        for target in config['targets']:
            state['seen'].pop(str(target['lpu_id']), None)
            state['paused'].pop(str(target['lpu_id']), None)
        state['last_scan'] = None
        save_state(state)

        run_id = uuid4().hex
        status = {'id': run_id, 'phase': 'starting', 'started_at': time.time()}
        _write_json(STATUS, status)
        args = [sys.executable]
        if not getattr(sys, 'frozen', False):
            args.append(str(RESOURCES / 'desktop_app.py'))
        args.extend(('--worker', run_id))
        options = (
            {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
            if sys.platform == 'win32' else {'start_new_session': True}
        )
        try:
            subprocess.Popen(
                args, cwd=ROOT, env={**os.environ, 'PYINSTALLER_RESET_ENVIRONMENT': '1'},
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                **options,
            )
        except OSError:
            _write_json(STATUS, {**status, 'phase': 'failed'})
            raise


class DesktopNotifier(Notifier):
    def _print(self, kind: str, text: str) -> None:
        log.info('%s %s', kind, text)
        if kind in ('OK', 'WARN', 'ERR'):
            self._event(kind, text)

    def broadcast(self, text: str, kind: str = 'INFO') -> None:
        if kind not in ('OK', 'WARN', 'ERR'):
            self._event(kind, text)
        super().broadcast(text, kind)

    @staticmethod
    def _event(kind: str, text: str) -> None:
        entries = _read_json(EVENTS, default=[])
        entries.append({
            'time': datetime.now().isoformat(timespec='seconds'),
            'kind': kind, 'text': text,
        })
        _write_json(EVENTS, entries[-100:])


def run_worker(run_id: str) -> int:
    try:
        with process_lock(WORKER_LOCK):
            status = _read_json(STATUS, default={})
            if status.get('id') != run_id or status.get('phase') != 'starting':
                return 1
            if _stop_requested(run_id):
                _write_json(STATUS, {**status, 'phase': 'stopped'})
                return 0
            _write_json(STATUS, {**status, 'phase': 'running'})
            LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
            handler = RotatingFileHandler(
                LOG_DIR / 'desktop.log', maxBytes=300_000, backupCount=2, encoding='utf-8',
            )
            logging.basicConfig(level=logging.INFO, handlers=[handler],
                                format='%(asctime)s %(levelname)s %(message)s')
            logging.getLogger('urllib3').setLevel(logging.WARNING)
            try:
                cfg = load_config()
                client = GorzdravClient(
                    verify=build_bundle(), timeout=15,
                    direct_interface=cfg['http'].get('direct_interface'),
                )
                notifier = DesktopNotifier(cfg)
                should_stop = lambda: _stop_requested(run_id)
                booker = Booker(client, load_profile(), notifier, should_stop=should_stop)
                state = load_state()
                before = len(state['booked'])
                code = Hunter(client, cfg, notifier, booker).run(state, should_stop=should_stop)
                phase = 'booked' if len(state['booked']) > before else 'stopped'
                _write_json(STATUS, {**status, 'phase': phase})
                return code
            except Exception:
                log.exception('Фоновый поиск завершился с ошибкой.')
                _write_json(STATUS, {**status, 'phase': 'failed'})
                return 1
    except BlockingIOError:
        return 1
