"""Одноразовый перенос конкретной записи через штатную операцию Горздрава."""
from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime

from .client import GorzdravError, Slot
from .config import record_booking, save_state

log = logging.getLogger(__name__)


def _same_visit(record: dict, lpu_id: int, specialty_id: str, visit_start: str) -> bool:
    specialty = record.get('specialityRendingConsultation') or {}
    return (
        str(record.get('lpuId')) == str(lpu_id)
        and str(specialty.get('id')) == specialty_id
        and str(record.get('visitStart') or '')[:19] == visit_start[:19]
    )


def _original(records: list[dict], target: dict) -> dict | None:
    source = target['reschedule_from']
    return next(
        (record for record in records
         if str(record.get('appointmentId')) == source['appointment_id']
         and _same_visit(record, target['lpu_id'], source['specialty_id'], source['visit_start'])),
        None,
    )


def _destination_exists(records: list[dict], slot: Slot) -> bool:
    return any(
        _same_visit(record, slot.lpu_id, slot.specialty_id, slot.visit_start)
        and str((record.get('doctorRendingConsultation') or {}).get('id')) == slot.doctor_id
        for record in records
    )


class Rescheduler:
    def __init__(self, client, booker, notifier, *, persist_state=None):
        self.client = client
        self.booker = booker
        self.notifier = notifier
        self.persist_state = persist_state

    def submit(self, state: dict, target: dict, slot: Slot, *, dry_run: bool) -> bool:
        if dry_run:
            self.notifier.slot('Пробный перенос\n' + self.booker.card(target, slot))
            return False
        if not target['auto_book'] or not (self.booker.profile or {}).get('consent_booking'):
            log.info('Есть талон для переноса. Для действия нужно явное согласие в профиле.')
            return False

        source = target['reschedule_from']
        source_id = source['appointment_id']
        patient_id = self.booker.patient_id(target['lpu_id'])
        records = self.client.patient_appointments(target['lpu_id'], patient_id) if patient_id else []
        old = _original(records, target)
        if (old is None or old.get('isAppointmentByReferral') or old.get('referralId')
                or _destination_exists(records, slot)):
            state['reschedules'][source_id] = {'status': 'blocked'}
            (self.persist_state or save_state)(state)
            self.notifier.broadcast(
                'Перенос остановлен. Проверь исходный и новый талоны в списке записей Горздрава.',
                kind='WARN',
            )
            return True
        should_stop = getattr(self.booker, 'should_stop', None)
        if should_stop is not None and should_stop():
            return False

        # Состояние сохраняется до POST: после сбоя или перезапуска разрешено только читать.
        transfer = {'status': 'submitted', 'slot': asdict(slot)}
        state['reschedules'][source_id] = transfer
        (self.persist_state or save_state)(state)
        try:
            self.client.appointment_change(
                lpu_id=target['lpu_id'], patient_id=patient_id,
                previous_id=source_id, slot_id=slot.id,
            )
        except GorzdravError as error:
            log.warning('Горздрав вернул код %s при переносе. Проверяю записи.', error.code)
            if error.code in (39, 645):
                transfer['slot_unavailable'] = True
                (self.persist_state or save_state)(state)
        return self._check_portal(state, target, patient_id)

    def reconcile(self, state: dict, target: dict, *, dry_run: bool) -> bool:
        transfer = state['reschedules'][target['reschedule_from']['appointment_id']]
        if transfer['status'] != 'submitted':
            return True
        if dry_run:
            log.info('Ожидается подтверждение предыдущего запроса на перенос.')
            return False
        patient_id = self.booker.patient_id(target['lpu_id'])
        if not patient_id:
            self._warn_pending(state, transfer)
            return False
        return self._check_portal(state, target, patient_id)

    def _check_portal(self, state: dict, target: dict, patient_id: str) -> bool:
        source_id = target['reschedule_from']['appointment_id']
        transfer = state['reschedules'][source_id]
        slot = Slot(**transfer['slot'])
        try:
            records = self.client.patient_appointments(target['lpu_id'], patient_id)
        except GorzdravError:
            self._warn_pending(state, transfer)
            raise
        old = _original(records, target)
        new = _destination_exists(records, slot)
        if new and old is None:
            transfer['status'] = 'completed'
            record_booking(state, target, slot)
            (self.persist_state or save_state)(state)
            self.notifier.broadcast('✅ ПРИЁМ ПЕРЕНЕСЁН\n' + self.booker.card(target, slot), kind='OK')
            self.notifier.desktop_notify('Приём перенесён', slot.visit_start)
            log.info('Горздрав подтвердил перенос на %s.', slot.visit_start)
            return True
        if transfer.get('slot_unavailable') and old is not None and not new:
            seen = state['seen'].setdefault(str(target['lpu_id']), {})
            seen[f'{slot.doctor_id}:{slot.id}'] = datetime.now().isoformat(timespec='seconds')
            del state['reschedules'][source_id]
            (self.persist_state or save_state)(state)
            log.info('Талон уже занят. Исходная запись на месте, продолжаю поиск.')
            return False
        self._warn_pending(state, transfer)
        log.info('Результат переноса ещё не подтверждён списком записей.')
        return False

    def _warn_pending(self, state: dict, transfer: dict) -> None:
        if not transfer.get('warned'):
            transfer['warned'] = True
            (self.persist_state or save_state)(state)
            self.notifier.broadcast(
                'Горздрав пока не подтвердил перенос. Скрипт проверяет список записей. '
                'Проверить вручную можно на https://gorzdrav.spb.ru/service-cancel',
                kind='WARN',
            )
