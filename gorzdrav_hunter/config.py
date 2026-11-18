"""Конфиг (config.json), профиль ПДн (profile.json) и состояние (state.json).

- config.json  — цели и поведение; может содержать токен бота → не в git.
- profile.json — ФИО и дата рождения (ПДн). Права 600, не в git.
- state.json   — кэш виденных слотов и результаты; можно удалять в любой момент.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / 'config.json'
PROFILE_PATH = ROOT / 'profile.json'
STATE_PATH = ROOT / 'state.json'
LOG_DIR = ROOT / 'logs'

DEFAULTS = {
    'poll_interval_sec': 60,
    'http': {
        'timeout_sec': 30,
        'sleep_between_requests_sec': 1.0,
        'insecure_tls': False,
    },
    'telegram': {
        'enabled': False,
        'bot_token': '',
        'chat_id': '',
        'api_url': 'https://api.telegram.org',
    },
    'notify': {
        'desktop_notification': False,
    },
    'auto_book': {
        'enabled': False,
        'stop_after_booking': True,
        'stop_if_same_specialty': True,
    },
    'heartbeat_hours': 0,
}


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


def _write_json(path: Path, data) -> None:
    handle = tempfile.NamedTemporaryFile(
        mode='w', dir=path.parent, encoding='utf-8', delete=False,
    )
    tmp = Path(handle.name)
    try:
        with handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------- config

def load_config(*, require_targets: bool = True) -> dict:
    cfg = _read_json(CONFIG_PATH)
    if cfg is None:
        raise FileNotFoundError(
            f'Нет {CONFIG_PATH}. Скопируй config.example.json → config.json.'
        )
    merged = json.loads(json.dumps(DEFAULTS))  # глубокая копия дефолтов
    for section, value in cfg.items():
        if section in ('targets',) or not isinstance(value, dict):
            merged[section] = value
        else:
            merged.setdefault(section, {})
            merged[section].update(value)
    merged.setdefault('targets', [])
    if require_targets and not merged['targets']:
        raise ValueError('В config.json пуст targets — добавь хотя бы одну цель '
                         '(см. config.example.json или `explore`).')
    merged['targets'] = [normalize_target(t, merged) for t in merged['targets']]
    interval = float(merged['poll_interval_sec'])
    if interval < 30:
        log.warning('poll_interval_sec=%s < 30 — портал просит не чаще раза '
                    'в 30 секунд, ставлю 30.', interval)
        merged['poll_interval_sec'] = 30
    return merged


def normalize_target(t: dict, cfg: dict) -> dict:
    lpu_id = int(t['lpu_id'])
    date_from = t.get('book_date_from')
    date_to = t.get('book_date_to')
    if not date_from:
        raise ValueError(f'Цель lpu_id={lpu_id}: укажи book_date_from (ГГГГ-ММ-ДД).')
    start = datetime.strptime(date_from, '%Y-%m-%d').date()
    end = datetime.strptime(date_to, '%Y-%m-%d').date() if date_to is not None else None
    if end is not None and start > end:
        raise ValueError(f'Цель lpu_id={lpu_id}: book_date_from позже book_date_to')
    contains = t.get('specialty_name_contains') or []
    if isinstance(contains, str):
        contains = [contains]
    ids = [str(x) for x in (t.get('specialty_ids') or [])]
    if not ids and not contains:
        raise ValueError(f'Цель lpu_id={lpu_id}: укажи специальность через ./hunt.sh explore.')
    doctor_ids = [str(x) for x in (t.get('doctor_ids') or [])]
    ab = cfg.get('auto_book', {})
    return {
        'lpu_id': lpu_id,
        'lpu_name': t.get('lpu_name') or f'ЛПУ {lpu_id}',
        'lpu_phone': t.get('lpu_phone'),
        'district_id': int(t['district_id']) if t.get('district_id') else None,
        'specialty_ids': ids,
        'specialty_name_contains': [c.lower() for c in contains],
        'doctor_ids': doctor_ids,
        'book_date_from': start.isoformat(),
        'book_date_to': end.isoformat() if end is not None else None,
        'notify_out_of_window': bool(t.get('notify_out_of_window', True)),
        'auto_book': bool(t.get('auto_book', ab.get('enabled', False))),
        'stop_if_same_specialty': bool(
            t.get('stop_if_same_specialty', ab.get('stop_if_same_specialty', True))
        ),
    }


def format_date_window(target: dict) -> str:
    start = target['book_date_from']
    end = target['book_date_to']
    return f'{start}…{end}' if end is not None else f'с {start}'


# ---------------------------------------------------------------- profile

def load_profile() -> dict | None:
    """Профиль ПДн или None, если пользователь его ещё не заполнил."""
    if not PROFILE_PATH.exists():
        return None
    mode = PROFILE_PATH.stat().st_mode & 0o777
    if mode & 0o077:
        os.chmod(PROFILE_PATH, 0o600)
        log.warning('На profile.json были широкие права (%o) — исправил на 600.', mode)
    return _read_json(PROFILE_PATH)


def save_profile(profile: dict) -> None:
    _write_json(PROFILE_PATH, profile)


def profile_is_ready(profile: dict | None) -> bool:
    if not profile:
        return False
    need = ('last_name', 'first_name', 'birthdate')
    return all(str(profile.get(k) or '').strip() for k in need)


def birthdate_iso(dmy: str) -> str:
    """'01.02.1990' → '1990-02-01T00:00:00' (формат patientBirthdate)."""
    d = datetime.strptime(dmy.strip(), '%d.%m.%Y')
    return d.strftime('%Y-%m-%dT00:00:00')


def mask_name(profile: dict | None) -> str:
    if not profile_is_ready(profile):
        return '(профиль не заполнен)'
    last = (profile.get('last_name') or '?')
    first = (profile.get('first_name') or '?')
    mid = (profile.get('middle_name') or '')
    return f'{last} {first[:1]}. {mid[:1]}.' if mid else f'{last} {first[:1]}.'


# ---------------------------------------------------------------- state

def load_state() -> dict:
    state = _read_json(STATE_PATH, default={}) or {}
    state.setdefault('seen', {})        # lpu_id → {doctor:slotid: first_seen iso}
    state.setdefault('booked_ids', [])
    state.setdefault('booked', [])
    state.setdefault('found_cache', [])
    state.setdefault('found_cache_time', None)
    state.setdefault('last_scan', None)
    state.setdefault('paused', {})      # lpu_id → {reason, since}
    return state


def save_state(state: dict) -> None:
    _write_json(STATE_PATH, state)
