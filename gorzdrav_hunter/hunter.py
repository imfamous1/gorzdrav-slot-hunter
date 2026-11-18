"""Хантер: цикл опроса, дедуп слотов, окно дат, автозапись, уведомления.

Вежливость к городскому API (по ограничениям портала):
- опрос не чаще раза в 30 секунд (дефолт 60);
- пауза между запросами внутри скана (клиент, дефолт 1с);
- без параллельных запросов;
- при сбоях ЛПУ — экспоненциальная пауза, а не долбёжка.
"""
from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from .booker import Booker
from .client import GorzdravClient, GorzdravError, Slot, portal_link
from .config import format_date_window, record_booking, save_state
from .reschedule import Rescheduler

log = logging.getLogger(__name__)

SEEN_TTL_DAYS = 30


def _slot_from_api(lpu_id: int, spec: dict, doc: dict, raw: dict) -> Slot:
    return Slot(
        lpu_id=lpu_id,
        specialty_id=str(spec['id']),
        specialty_name=spec['name'],
        doctor_id=str(doc['id']),
        doctor_name=doc['name'],
        id=str(raw['id']),
        visit_start=raw['visitStart'],
        visit_end=raw.get('visitEnd') or '',
        room=raw.get('room'),
        number=raw.get('number'),
        address=raw.get('address'),
    )


class Hunter:
    def __init__(self, client: GorzdravClient, cfg: dict, notifier,
                 booker: Booker | None = None, *, persist_state=None):
        self.client = client
        self.cfg = cfg
        self.notifier = notifier
        self.booker = booker
        self.persist_state = persist_state
        self.rescheduler = Rescheduler(client, booker, notifier, persist_state=persist_state)
        self.state = None  # передаётся снаружи (cli), чтобы `slots` не сохранял

    # ------------------------------------------------------------ matching

    @staticmethod
    def _in_window(slot: Slot, target: dict) -> bool:
        d = slot.visit_start[:10]
        end = target['book_date_to']
        return target['book_date_from'] <= d and (end is None or d <= end)

    @staticmethod
    def _spec_matches(spec: dict, target: dict) -> bool:
        name = (spec.get('name') or '').lower()
        contains = target['specialty_name_contains']
        return str(spec['id']) in target['specialty_ids'] or any(c in name for c in contains)

    # ------------------------------------------------------------ scanning

    def collect_slots(self, target: dict) -> list[Slot]:
        """Все свободные слоты цели: специальности → врачи → appointments."""
        lpu_id = target['lpu_id']
        specs = [s for s in self.client.specialties(lpu_id)
                 if self._spec_matches(s, target)]
        if not specs:
            log.info('ЛПУ %s: выбранных специальностей сейчас нет в онлайн-записи.',
                     lpu_id)
            return []
        slots: list[Slot] = []
        for spec in specs:
            docs = self.client.doctors(lpu_id, spec['id'])
            if target['doctor_ids']:
                docs = [d for d in docs if str(d['id']) in target['doctor_ids']]
            for doc in docs:
                for raw in self.client.appointments(lpu_id, doc['id']):
                    slots.append(_slot_from_api(lpu_id, spec, doc, raw))
        return slots

    def scan_once(self, state: dict, *, persist: bool = True, notify: bool = True,
                  dry_run: bool = False, interactive: bool = False) -> bool:
        """Один проход по всем целям. Возвращает True, если надо остановиться.

        persist=False сохраняет результаты только в памяти (dry-run).
        """
        scan_time = datetime.now().isoformat(timespec='seconds')
        last_error = None
        succeeded = False
        stop = False
        for target in self.cfg['targets']:
            try:
                stop = self._scan_target(state, target, persist=persist,
                                         notify=notify, dry_run=dry_run,
                                         interactive=interactive) or stop
                succeeded = True
            except GorzdravError as e:
                last_error = e
                lvl = log.warning if e.transient else log.error
                lvl('ЛПУ %s: %s (errorCode %s)', target['lpu_id'],
                    e.friendly, e.code)
        if not succeeded and last_error is not None:
            raise last_error
        state['last_scan'] = scan_time
        state['found_cache_time'] = scan_time
        if persist:
            (self.persist_state or save_state)(state)
        return stop

    def _scan_target(self, state, target, *, persist, notify, dry_run,
                     interactive) -> bool:
        lpu_key = str(target['lpu_id'])
        source = target.get('reschedule_from')
        if source:
            if source['appointment_id'] in state.setdefault('reschedules', {}):
                return self.rescheduler.reconcile(state, target, dry_run=dry_run or not persist)
            seen = state['seen'].setdefault(lpu_key, {})
            candidate = min(
                (slot for slot in self.collect_slots(target)
                 if slot.specialty_id == source['specialty_id'] and self._in_window(slot, target)
                 and self._seen_key(slot) not in seen and slot.id not in state['booked_ids']),
                key=lambda slot: slot.visit_start,
                default=None,
            )
            if candidate is None:
                log.info('ЛПУ %s: для переноса пока нет подходящих талонов (%s).',
                         target['lpu_id'], format_date_window(target))
                return False
            return self.rescheduler.submit(state, target, candidate, dry_run=dry_run or not persist)

        paused = state['paused'].get(lpu_key)
        if paused:
            if not dry_run and self._try_unpause(state, target):
                log.info('ЛПУ %s: автозапись возобновлена.', target['lpu_id'])
            else:
                log.info('ЛПУ %s: автозапись на паузе (%s) — сканирую в режиме '
                         'уведомлений.', target['lpu_id'], paused['reason'])

        slots = self.collect_slots(target)
        seen = state['seen'].setdefault(lpu_key, {})
        booked_ids = set(state['booked_ids'])

        fresh = [s for s in slots if self._seen_key(s) not in seen
                 and s.id not in booked_ids]
        in_win = [s for s in fresh if self._in_window(s, target)]
        out_win = [s for s in fresh if not self._in_window(s, target)]

        if not fresh:
            log.info('ЛПУ %s (%s): слотов нет (%s шт. всего).',
                     target['lpu_id'], target['lpu_name'], len(slots))
            return False

        skip_seen: set[str] = set()
        if in_win:
            self._cache_found(state, in_win, target, in_window=True)
            if notify:
                self.notifier.slot(
                    f'🎯 НОВЫЙ СЛОТ ({format_date_window(target)})\n'
                    + self.booker.card(target, in_win[0])
                )
            log.info('ЛПУ %s: %d новый слот(ов) в окне.', target['lpu_id'], len(in_win))

            if self._auto_book_active(state, target):
                earliest = min(in_win, key=lambda s: s.visit_start)
                result = self.booker.book(
                    target, earliest,
                    dry_run=dry_run,
                    consent=bool((self.booker.profile or {}).get('consent_booking')),
                    interactive=interactive,
                )
                if result == 'booked':
                    record_booking(state, target, earliest)
                    self._mark_seen(state, target, slots)
                    return bool(self.cfg['auto_book'].get('stop_after_booking', True))
                if result in ('exists', 'declined'):
                    self._mark_seen(state, target, slots)
                    return result == 'exists'
                if result == 'no_patient':
                    state['paused'][lpu_key] = {
                        'reason': 'no_patient',
                        'since': datetime.now().isoformat(timespec='seconds'),
                    }
                elif result == 'unverified':
                    state['booked_ids'].append(earliest.id)
                elif result in ('dry_run', 'no_profile', 'no_consent', 'vanished'):
                    pass
                if result in ('transient', 'error'):
                    # слот остаётся «не виденным» — повторим попытку в следующий цикл
                    skip_seen.add(earliest.id)
            elif notify and not dry_run:
                links = self.booker.link(target, in_win[0])
                card = self.booker.card(target, in_win[0])
                self.notifier.broadcast(f'Слот в окне\n{card}\n{links}', kind='SLOT')

        if out_win and target['notify_out_of_window'] and notify:
            sample = ', '.join(s.visit_start[:16].replace('T', ' ')
                               for s in sorted(out_win,
                                               key=lambda s: s.visit_start)[:3])
            self.notifier.info(
                f'{target["lpu_name"]}: появились слоты вне окна '
                f'({len(out_win)} шт., первые: {sample}). Автозапись их не берёт — '
                f'окно {format_date_window(target)}.'
            )
            self._cache_found(state, out_win, target, in_window=False)

        # помечаем всё виденным, кроме слотов с незавершённой попыткой записи
        seen = state['seen'][lpu_key]
        now = datetime.now().isoformat(timespec='seconds')
        for s in slots:
            if s.id not in skip_seen:
                seen[self._seen_key(s)] = now
        self._prune_seen(state, lpu_key)
        return False

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _seen_key(s: Slot) -> str:
        return f'{s.doctor_id}:{s.id}'

    def _mark_seen(self, state, target, slots: list[Slot]) -> None:
        seen = state['seen'].setdefault(str(target['lpu_id']), {})
        now = datetime.now().isoformat(timespec='seconds')
        for s in slots:
            seen[self._seen_key(s)] = now

    def _prune_seen(self, state, lpu_key: str) -> None:
        cutoff = (datetime.now() - timedelta(days=SEEN_TTL_DAYS)).isoformat(
            timespec='seconds')
        seen = state['seen'].get(lpu_key, {})
        for k in [k for k, ts in seen.items() if ts < cutoff]:
            seen.pop(k, None)

    @staticmethod
    def _cache_found(state, slots: list[Slot], target: dict, in_window: bool) -> None:
        cache = state.setdefault('found_cache', [])
        for s in slots:
            cache.append({
                'lpu_id': target['lpu_id'],
                'lpu_name': target['lpu_name'],
                'lpu_phone': target.get('lpu_phone'),
                'district_id': target.get('district_id'),
                'specialty_id': s.specialty_id,
                'specialty_name': s.specialty_name,
                'doctor_id': s.doctor_id,
                'doctor_name': s.doctor_name,
                'slot_id': s.id,
                'visit_start': s.visit_start,
                'visit_end': s.visit_end,
                'room': s.room,
                'number': s.number,
                'address': s.address,
                'in_window': in_window,
                'cached_at': datetime.now().isoformat(timespec='seconds'),
            })
        del cache[:-50]  # храним последние 50 находок

    def _auto_book_active(self, state, target) -> bool:
        if self.booker is None:
            return False
        if not target['auto_book']:
            return False
        if str(target['lpu_id']) in state['paused']:
            return False
        return True

    def _try_unpause(self, state, target) -> bool:
        """Если пауза из-за no_patient — пробуем снова найти карточку."""
        info = state['paused'].get(str(target['lpu_id']))
        if not info or info.get('reason') != 'no_patient':
            return False
        pid = self.booker.patient_id(target['lpu_id'])
        if pid:
            state['paused'].pop(str(target['lpu_id']), None)
            return True
        return False

    # ------------------------------------------------------------ loop

    def run(self, state, dry_run: bool = False, interactive: bool = False,
            max_backoff_sec: float = 600.0, *,
            should_stop: Callable[[], bool] | None = None) -> int:
        interval = float(self.cfg['poll_interval_sec'])
        heartbeat_hours = float(self.cfg.get('heartbeat_hours') or 0)
        last_heartbeat = time.monotonic()
        failures = 0
        log.info(
            'Хантер запущен: %s%s',
            ', '.join(f"{t['lpu_name']} ({format_date_window(t)})" for t in self.cfg['targets']),
            ' (DRY-RUN)' if dry_run else '',
        )
        try:
            while should_stop is None or not should_stop():
                try:
                    stop = self.scan_once(state, dry_run=dry_run, persist=not dry_run,
                                          interactive=interactive)
                    failures = 0
                    if stop:
                        log.info('Фоновый опрос завершён.')
                        return 0
                except GorzdravError as e:
                    failures += 1
                    self._on_failure(failures, e)
                except Exception as e:  # noqa: BLE001 — цикл обязан выживать
                    failures += 1
                    log.exception('Неожиданная ошибка скана: %s', e)
                    self._on_failure(failures, e)

                if heartbeat_hours > 0 and \
                        time.monotonic() - last_heartbeat >= heartbeat_hours * 3600:
                    last_heartbeat = time.monotonic()
                    self.notifier.info('❤️ Жив: последний скан '
                                       f"{state.get('last_scan') or '—'}")

                wait = interval * (2 ** min(failures, 4)) if failures else interval
                wait = max(30.0, min(wait * random.uniform(0.9, 1.1), max_backoff_sec))
                log.debug('Пауза %.0fс', wait)
                if should_stop is None:
                    time.sleep(wait)
                    continue
                deadline = time.monotonic() + wait
                while (remaining := deadline - time.monotonic()) > 0:
                    if should_stop():
                        return 0
                    time.sleep(min(remaining, 1.0))
            return 0
        except KeyboardInterrupt:
            self.notifier.info('Остановлено вручную (Ctrl+C). Состояние сохранено.')
            return 130

    def _on_failure(self, failures: int, error: Exception) -> None:
        details = (f'{error.friendly} (errorCode {error.code})'
                   if isinstance(error, GorzdravError) else type(error).__name__)
        if failures in (3, 10, 30):
            self.notifier.warn(
                f'Не удаётся завершить скан: {details}. Продолжаю с увеличенной паузой.')
        log.warning('Скан не удался: %s, подряд неудач: %d', details, failures)
