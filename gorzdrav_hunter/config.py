"""Конфиг (config.json), профиль ПДн (profile.json) и состояние (state.json).

- config.json  — цели и поведение; может содержать токен бота → не в git.
- profile.json — ФИО и дата рождения (ПДн). Права 600, не в git.
- state.json   — кэш виденных слотов и результаты; можно удалять в любой момент.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from .paths import data_path

if TYPE_CHECKING:
    from .client import Slot

log = logging.getLogger(__name__)

ROOT = data_path()
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
        for attempt in range(4):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if sys.platform != 'win32' or attempt == 3:
                    raise
                time.sleep(0.05)
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


def parse_book_time(value: str | None) -> str | None:
    if value is None or value == '':
        return None
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value):
        raise ValueError('Укажи время в формате ЧЧ:ММ, например 09:30.')
    return value


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
    time_from = parse_book_time(t.get('book_time_from'))
    time_to = parse_book_time(t.get('book_time_to'))
    if time_from and time_to and time_from > time_to:
        raise ValueError(f'Цель lpu_id={lpu_id}: book_time_from позже book_time_to')
    contains = t.get('specialty_name_contains') or []
    if isinstance(contains, str):
        contains = [contains]
    ids = [str(x) for x in (t.get('specialty_ids') or [])]
    if not ids and not contains:
        raise ValueError(f'Цель lpu_id={lpu_id}: укажи специальность через ./hunt.sh explore.')
    doctor_ids = [str(x) for x in (t.get('doctor_ids') or [])]
    ab = cfg.get('auto_book', {})
    source = t.get('reschedule_from')
    if source is not None:
        visit_start = datetime.fromisoformat(source['visit_start'])
        appointment_id = str(source['appointment_id']).strip() if source['appointment_id'] else ''
        specialty_id = str(source['specialty_id']).strip() if source['specialty_id'] else ''
        if not appointment_id or not specialty_id or visit_start.date() >= start:
            raise ValueError(f'Цель lpu_id={lpu_id}: проверь исходный талон и даты переноса.')
        source = {
            'appointment_id': appointment_id,
            'specialty_id': specialty_id,
            'visit_start': visit_start.isoformat(timespec='seconds'),
        }
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
        'book_time_from': time_from,
        'book_time_to': time_to,
        'reschedule_from': source,
        'notify_out_of_window': bool(t.get('notify_out_of_window', True)),
        'auto_book': bool(t.get('auto_book', ab.get('enabled', False))),
        'stop_if_same_specialty': bool(
            t.get('stop_if_same_specialty', ab.get('stop_if_same_specialty', True))
        ),
    }


def format_date_window(target: dict) -> str:
    start = target['book_date_from']
    end = target['book_date_to']
    dates = f'{start}…{end}' if end is not None else f'с {start}'
    hours = format_time_window(target)
    return f'{dates}, ежедневно {hours}' if hours else dates


def format_time_window(target: dict) -> str:
    start, end = target.get('book_time_from'), target.get('book_time_to')
    if start and end:
        return f'{start}–{end}'
    if start:
        return f'с {start}'
    return f'до {end}' if end else ''


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
    state.setdefault('reschedules', {})
    return state


def save_state(state: dict) -> None:
    _write_json(STATE_PATH, state)


def record_booking(state: dict, target: dict, slot: Slot) -> None:
    if slot.id not in state['booked_ids']:
        state['booked_ids'].append(slot.id)
    state['booked'].append({
        'slot_id': slot.id,
        'visit_start': slot.visit_start,
        'doctor': slot.doctor_name,
        'lpu': target['lpu_name'],
        'at': datetime.now().isoformat(timespec='seconds'),
    })
