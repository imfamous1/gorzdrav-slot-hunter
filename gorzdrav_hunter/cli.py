"""Командный интерфейс gorzdrav-slot-hunter.

    python -m gorzdrav_hunter hunt              # цикл охоты (основная команда)
    python -m gorzdrav_hunter slots             # разовый просмотр слотов
    python -m gorzdrav_hunter status            # что настроено, что видели
    python -m gorzdrav_hunter explore           # добавить поликлинику интерактивно
    python -m gorzdrav_hunter book N            # вручную записать слот №N из slots
    python -m gorzdrav_hunter test-telegram     # проверка уведомлений
    python -m gorzdrav_hunter forget            # удалить профиль (ПДн) и состояние
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import asdict
from datetime import datetime, timedelta

from . import __version__
from .booker import Booker
from .client import GorzdravClient, GorzdravError, Slot
from .config import (
    LOG_DIR, PROFILE_PATH, ROOT, STATE_PATH, _read_json, _write_json,
    format_date_window, load_config, load_profile, load_state, profile_is_ready, record_booking,
    save_state,
)
from .hunter import Hunter
from .notify import Notifier
from .process_lock import process_lock
from .tls import resolve_verify

log = logging.getLogger('gorzdrav')
CACHE_DIR = ROOT / '.cache'
SLOTS_PATH = CACHE_DIR / 'manual-slots.json'
LOCK_PATH = CACHE_DIR / 'hunter.lock'


def _setup_logging(log_file: str | None, verbose: bool) -> None:
    fmt = '%(asctime)s | %(levelname)-7s | %(message)s'
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        LOG_DIR.mkdir(exist_ok=True)
        handlers.append(logging.FileHandler(LOG_DIR / log_file, encoding='utf-8'))
    logging.basicConfig(level=level, format=fmt, datefmt='%Y-%m-%d %H:%M:%S',
                        handlers=handlers)
    logging.getLogger('urllib3').setLevel(logging.WARNING)


def _build(args, *, require_targets: bool = True) -> tuple[GorzdravClient, dict, Notifier, Booker, Hunter]:
    cfg = load_config(require_targets=require_targets)
    verify = resolve_verify(args.insecure or cfg['http'].get('insecure_tls', False))
    client = GorzdravClient(
        verify=verify,
        timeout=float(cfg['http']['timeout_sec']),
        sleep_between=float(cfg['http']['sleep_between_requests_sec']),
        direct_interface=cfg['http'].get('direct_interface'),
    )
    notifier = Notifier(cfg)
    profile = load_profile()
    booker = Booker(client, profile, notifier)
    hunter = Hunter(client, cfg, notifier, booker)
    return client, cfg, notifier, booker, hunter


def cmd_hunt(args) -> int:
    _, cfg, notifier, booker, hunter = _build(args)
    state = load_state()
    dry = args.dry_run or args.no_book
    interactive = sys.stdin.isatty()
    if args.once:
        hunter.scan_once(state, dry_run=dry, persist=not dry, interactive=interactive)
        return 0
    profile = booker.profile
    if profile_is_ready(profile) and any(t['auto_book'] for t in cfg['targets']):
        consent = bool(profile.get('consent_booking'))
        if not consent and not dry and not interactive:
            notifier.warn('Автозапись без consent_booking=true в headless-режиме '
                          'работает только как уведомления.')
    return hunter.run(state, dry_run=dry, interactive=interactive)


def cmd_slots(args) -> int:
    _, cfg, _, _, hunter = _build(args)
    SLOTS_PATH.unlink(missing_ok=True)
    slots = sorted(
        ((slot, target) for target in cfg['targets'] for slot in hunter.collect_slots(target)),
        key=lambda item: item[0].visit_start,
    )
    CACHE_DIR.mkdir(exist_ok=True)
    _write_json(SLOTS_PATH, {
        'created_at': datetime.now().isoformat(),
        'slots': [asdict(slot) for slot, _ in slots],
    })
    if slots:
        print(f'Свободные талоны ({len(slots)})')
        for i, (slot, target) in enumerate(slots, 1):
            mark = '✓' if hunter._in_window(slot, target) else ' '
            room = f', каб. {slot.room}' if slot.room else ''
            when = slot.visit_start.replace('T', ' ')[:16]
            print(f' {i:3}{mark} {when}  {slot.doctor_name}{room} [{target["lpu_name"]}]')
        print('\n✓ — подходит по датам и времени. Ручная запись: ./hunt.sh book <номер>')
    else:
        print('Свободных талонов сейчас нет.')
    return 0


def cmd_book(args) -> int:
    if not sys.stdin.isatty():
        print('Для ручной записи запусти команду из терминала и подтверди талон.')
        return 1
    snapshot = _read_json(SLOTS_PATH)
    if not snapshot or datetime.now() - datetime.fromisoformat(snapshot['created_at']) >= timedelta(minutes=15):
        print('Список талонов отсутствует или устарел. Выполни ./hunt.sh slots.')
        return 1
    if not 1 <= args.n <= len(snapshot['slots']):
        print('Номер не найден в последнем выводе ./hunt.sh slots.')
        return 1
    slot = Slot(**snapshot['slots'][args.n - 1])
    _, cfg, _, booker, _ = _build(args)
    target = next((t for t in cfg['targets'] if t['lpu_id'] == slot.lpu_id), None)
    if target is None:
        print('Поликлиника больше не входит в цели. Выполни ./hunt.sh slots.')
        return 1
    result = booker.book(target, slot, interactive=True)
    if result in ('booked', 'unverified'):
        state = load_state()
        if result == 'booked':
            record_booking(state, target, slot)
        elif slot.id not in state['booked_ids']:
            state['booked_ids'].append(slot.id)
        save_state(state)
    return 0 if result == 'booked' else 1


def cmd_status(args) -> int:
    cfg = load_config(require_targets=False)
    state = load_state()
    print(f'gorzdrav-slot-hunter {__version__}')
    print(f'Интервал опроса: {cfg["poll_interval_sec"]}с | '
          f'автозапись: {"ВКЛ" if cfg["auto_book"]["enabled"] else "выкл"} | '
          f'stop_after_booking: {cfg["auto_book"]["stop_after_booking"]}')
    tg = cfg['telegram']
    tg_status = 'выключен' if not tg['enabled'] else (
        'настроен' if tg.get('bot_token') and tg.get('chat_id') else 'нужны bot_token и chat_id'
    )
    print(f'Telegram: {tg_status}')
    profile = load_profile()
    print(f'Профиль: {"заполнен" if profile_is_ready(profile) else "не заполнен"} | '
          f'consent_booking: {bool(profile and profile.get("consent_booking"))}')
    print('Цели:')
    if not cfg['targets']:
        print('  Добавь цель через ./hunt.sh explore')
    for t in cfg['targets']:
        print(f'  • {t["lpu_name"]} (ЛПУ {t["lpu_id"]}) | '
              f'окно {format_date_window(t)} | '
              f'автозапись: {"вкл" if t["auto_book"] else "выкл"} | '
              f'пауза: {state["paused"].get(str(t["lpu_id"]), {}).get("reason", "нет")}')
        if source := t.get('reschedule_from'):
            transfer = state['reschedules'].get(source['appointment_id'], {})
            statuses = {
                'submitted': 'проверяем результат на портале',
                'completed': 'подтверждён',
                'blocked': 'остановлен, нужна проверка записи',
            }
            status = statuses.get(transfer.get('status'), 'ищем подходящий талон')
            old = source['visit_start'].replace('T', ' ')[:16]
            print(f'    Перенос приёма от {old}: {status}')
    print(f'Последний успешный опрос: {state.get("last_scan") or "—"}')
    seen_total = sum(len(v) for v in state['seen'].values())
    print(f'Виденных слотов в состоянии: {seen_total} | '
          f'подтверждённых записей: {len(state["booked"])}')
    for b in state['booked']:
        print(f'  ✅ {b["visit_start"]} {b["doctor"]} @ {b["lpu"]}')
    return 0


def cmd_test_telegram(args) -> int:
    notifier = Notifier(load_config(require_targets=False))
    ok = notifier.telegram('✅ gorzdrav-slot-hunter: проверка связи. '
                           'Если видишь это — уведомления настроены.')
    print('Telegram: ' + ('улетело ✅' if ok else 'не ушло ❌ (см. warning выше)'))
    return 0 if ok else 1


def cmd_forget(args) -> int:
    answer = input('Удалить профиль, историю и список талонов? [y/N] ').strip().lower()
    if answer in ('y', 'yes', 'д', 'да'):
        for p in (PROFILE_PATH, STATE_PATH, SLOTS_PATH):
            if p.exists():
                p.unlink()
                print(f'удалён {p.name}')
        return 0
    print('Отмена.')
    return 1


def cmd_explore(args) -> int:
    client, _, _, _, _ = _build(args, require_targets=False)
    from .explore import run_explore
    run_explore(client)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog='gorzdrav_hunter',
        description='Ловец свободных талонов на gorzdrav.spb.ru (личное пользование)')
    ap.add_argument('--insecure', action='store_true',
                    help='отключить проверку TLS (не надо)')
    ap.add_argument('-v', '--verbose', action='store_true')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('hunt', help='цикл охоты за слотами')
    p.add_argument('--once', action='store_true', help='один скан и выход')
    p.add_argument('--dry-run', action='store_true',
                   help='находить слоты, но НЕ записывать')
    p.add_argument('--no-book', action='store_true', help='то же, что --dry-run')
    p.add_argument('--log-file', default=None, help='файл лога в logs/')
    p.set_defaults(fn=cmd_hunt, log_file=None)

    p = sub.add_parser('slots', help='разово показать текущие свободные слоты')
    p.set_defaults(fn=cmd_slots)

    p = sub.add_parser('book', help='записаться на слот №N из вывода slots')
    p.add_argument('n', type=int)
    p.set_defaults(fn=cmd_book)

    p = sub.add_parser('status', help='показать цели, профиль, состояние')
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser('explore', help='интерактивно добавить поликлинику')
    p.set_defaults(fn=cmd_explore)

    p = sub.add_parser('test-telegram', help='проверить уведомления в Telegram')
    p.set_defaults(fn=cmd_test_telegram)

    p = sub.add_parser('forget', help='удалить профиль и локальную историю')
    p.set_defaults(fn=cmd_forget)

    args = ap.parse_args(argv)
    _setup_logging(getattr(args, 'log_file', None), args.verbose)

    try:
        if args.cmd in ('hunt', 'slots', 'book', 'forget'):
            try:
                with process_lock(LOCK_PATH):
                    return args.fn(args)
            except BlockingIOError:
                log.error('Скрипт уже работает. Останови текущий запуск перед этой командой.')
                return 1
        return args.fn(args)
    except GorzdravError as error:
        log.error('%s (код %s)', error.friendly, error.code)
        return 1
    except (FileNotFoundError, ValueError, KeyError, OSError) as error:
        log.error('%s', error)
        return 1


if __name__ == '__main__':
    sys.exit(main())
