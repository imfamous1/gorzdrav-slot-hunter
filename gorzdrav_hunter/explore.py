"""Интерактивный выбор цели: район → поликлиника → специальность → врач.

Используется, когда нужно добавить поликлинику в config.json без правки руками.
"""
from __future__ import annotations

from datetime import date

from .client import GorzdravClient, GorzdravError
from .config import CONFIG_PATH, _read_json, _write_json, format_date_window, normalize_target


def _ask(prompt: str, default: str = '') -> str:
    val = input(f'{prompt}' + (f' [{default}]' if default else '') + ': ').strip()
    return val or default


def _choose_numbered(items, label_fn, prompt) -> int | None:
    for i, it in enumerate(items, 1):
        print(f'  {i:3}. {label_fn(it)}')
    raw = _ask(prompt + ' (номер, пусто — отмена)')
    if not raw.isdigit() or not (1 <= int(raw) <= len(items)):
        return None
    return int(raw) - 1


def run_explore(client: GorzdravClient) -> None:
    print('=== Добавление цели в config.json ===')
    districts = client.districts()
    idx = _choose_numbered(
        districts, lambda d: f"{d['id']:>2}. {d['name']}", 'Район')
    if idx is None:
        return
    district = districts[idx]

    lpus = [l for l in client.lpus(int(district['id'])) if l.get('isActive')]
    print(f"Поликлиники района «{district['name']}»: {len(lpus)}")
    idx = _choose_numbered(
        lpus,
        lambda l: f"{l['id']:>5}. {l['lpuShortName']} — {l['address']}",
        'Поликлиника')
    if idx is None:
        return
    lpu = lpus[idx]
    lpu_id = int(lpu['id'])

    try:
        specs = client.specialties(lpu_id)
    except GorzdravError as e:
        print(f'Специальности не получились: {e.friendly}')
        return
    if not specs:
        print('В этой поликлинике сейчас нет онлайн-записи вообще (errorCode 37).')
        return
    needle = _ask('Фильтр специальности (пусто — показать все)').lower()
    matched = [s for s in specs if needle in (s['name'] or '').lower()]
    if not matched:
        print('Такой специальности нет. Доступные:')
        for s in specs:
            print(f"   {s['id']:>8}. {s['name']}")
        return
    idx = _choose_numbered(matched, lambda s: f"{s['id']:>8}. {s['name']}",
                           'Специальность')
    if idx is None:
        return
    spec = matched[idx]

    try:
        docs = client.doctors(lpu_id, spec['id'])
    except GorzdravError as e:
        docs = []
        print(f'Врачи не получились: {e.friendly} (ло́вим всех — ок.)')
    if docs:
        print('Врачи (пусто в вопросе = следить за всеми):')
        for d in docs:
            print(f"   {d['id']:>8}. {d['name']} "
                  f"(свободно: {d.get('freeTicketCount')}, ближайшая: "
                  f"{(d.get('nearestDate') or '—')[:16]})")
    doctor_ids_raw = _ask('id врачей через запятую (пусто — все)', '')
    doctor_ids = [x.strip() for x in doctor_ids_raw.split(',') if x.strip()]

    d_from = _ask('Дата ОТ (ГГГГ-ММ-ДД)', date.today().isoformat())
    d_to = _ask('Дата ДО (ГГГГ-ММ-ДД, пусто — без ограничения)') or None

    cfg = _read_json(CONFIG_PATH, default={}) or {}
    cfg.setdefault('targets', [])
    target = {
        'lpu_id': lpu_id,
        'lpu_name': lpu.get('lpuShortName') or f'ЛПУ {lpu_id}',
        'lpu_phone': lpu.get('phone') or None,
        'district_id': int(district['id']),
        'specialty_ids': [str(spec['id'])],
        'doctor_ids': doctor_ids,
        'book_date_from': d_from,
        'book_date_to': d_to,
    }
    normalized = normalize_target(target, cfg)
    cfg['targets'].append(target)
    _write_json(CONFIG_PATH, cfg)
    print(f"Цель добавлена в {CONFIG_PATH}: {lpu['lpuShortName']} / "
          f"{spec['name']} / {format_date_window(normalized)}")
