"""Запись через API: patient/search → appointment/create → контроль.

Все операции здесь трогают персональные данные и/или создают реальную запись:
- данные профиля уходят только на gorzdrav.spb.ru (как при записи руками);
- перед первой реальной записью — карточка и подтверждение (consent_booking);
- после create обязательно проверяем слот в списке записей пациента.
"""
from __future__ import annotations

from collections.abc import Callable
import logging
import time
from datetime import datetime

from .client import AttachmentDenied, GorzdravClient, GorzdravError, Slot, portal_link
from .config import birthdate_iso, profile_is_ready

log = logging.getLogger(__name__)

WD = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс']


def initials(last: str, first: str, middle: str = '') -> str:
    s = f'{last} {first[:1]}.'
    return f'{s} {middle[:1]}.' if middle else s


class Booker:
    def __init__(self, client: GorzdravClient, profile: dict | None, notifier, *,
                 should_stop: Callable[[], bool] | None = None, persist_profile=None):
        self.should_stop = should_stop
        self.persist_profile = persist_profile
        self.client = client
        self.profile = profile
        self.notifier = notifier

    # ------------------------------------------------------------ helpers

    def set_profile(self, profile: dict | None) -> None:
        self.profile = profile

    def card(self, target: dict, slot: Slot, *, include_patient: bool = False) -> str:
        """Формат карточки приёма для терминала и Telegram."""
        who = ''
        if include_patient and profile_is_ready(self.profile):
            p = self.profile
            who = (f"{initials(p['last_name'], p['first_name'], p.get('middle_name', ''))}, "
                   f"{p['birthdate']}\n")
        dt = datetime.strptime(slot.visit_start[:16], '%Y-%m-%dT%H:%M')
        when = dt.strftime('%d.%m') + f' ({WD[dt.weekday()]}) ' + dt.strftime('%H:%M')
        room = f', каб. {slot.room}' if slot.room else ''
        phone = f"\nТелефон поликлиники: {target['lpu_phone']}" if target.get('lpu_phone') else ''
        return (
            f'{who}'
            f'Поликлиника: {target["lpu_name"]}\n'
            f'{slot.specialty_name} — {slot.doctor_name}\n'
            f'Приём: {when}{room}'
            f'{phone}'
        )

    def link(self, target: dict, slot: Slot) -> str:
        return portal_link(
            target.get('district_id'), target['lpu_id'],
            slot.specialty_id, slot.doctor_id,
        )

    # ------------------------------------------------------------ patient

    def patient_id(self, lpu_id: int) -> str | None:
        """id медкарты в ЛПУ; кэш в profile.patient_ids, чтобы не гнать ФИО в сеть зря."""
        p = self.profile
        if not profile_is_ready(p):
            return None
        cache = p.setdefault('patient_ids', {})
        if str(lpu_id) in cache:
            return cache[str(lpu_id)]
        pid = self.client.patient_search(
            lpu_id,
            last_name=p['last_name'].strip(),
            first_name=p['first_name'].strip(),
            middle_name=(p.get('middle_name') or '').strip(),
            birthdate_iso=birthdate_iso(p['birthdate']),
            birthdate_dmy=p['birthdate'].strip(),
        )
        if pid:
            cache[str(lpu_id)] = pid
            from .config import save_profile  # локальный импорт — цикл не нужен
            (self.persist_profile or save_profile)(p)
            log.info('patientId для ЛПУ %s найден и закэширован.', lpu_id)
        return pid

    def existing_specialty_record(self, lpu_id: int, patient_id: str,
                                  name_part: str) -> dict | None:
        """Активная запись по этой же специальности (риск errorCode 751)."""
        try:
            records = self.client.patient_appointments(lpu_id, patient_id)
        except GorzdravError as e:
            log.warning('Не смог проверить существующие записи (%s) — '
                        'продолжаю, 751 обработается при create.', e.friendly)
            return None
        for r in records:
            spec = ((r.get('specialityRendingConsultation') or {}).get('name') or '')
            if name_part.lower() in spec.lower():
                return r
        return None

    # ------------------------------------------------------------ booking

    def book(
        self,
        target: dict,
        slot: Slot,
        dry_run: bool = False,
        consent: bool = False,
        interactive: bool = False,
    ) -> str:
        """Попытка записи. Возвращает исход: booked/unverified/dry_run/
        exists/no_patient/no_profile/vanished/error/transient."""
        if dry_run:
            text = f'DRY-RUN: нашёл слот, запись НЕ делаю.\n{self.card(target, slot)}'
            self.notifier.slot(text)
            return 'dry_run'

        if not profile_is_ready(self.profile):
            self.notifier.broadcast(
                f'Слот найден, но профиль не заполнен — запись не делаю.\n'
                f'{self.card(target, slot)}\n{self.link(target, slot)}',
                kind='SLOT',
            )
            return 'no_profile'

        if not consent and not interactive:
            # headless-режим без явного согласия — только уведомляем
            self.notifier.broadcast(
                f'Слот найден, но запись не подтверждена (consent_booking=false).\n'
                f'{self.card(target, slot)}\n{self.link(target, slot)}',
                kind='SLOT',
            )
            return 'no_consent'

        pid = self.patient_id(target['lpu_id'])
        if not pid:
            self.notifier.error(
                'Карточка пациента не найдена в этой поликлинике (620). '
                'Проверь ФИО/дату рождения в profile.json и прикрепление. '
                'Автозапись по этой цели приостановлена.'
            )
            return 'no_patient'

        # уже есть запись к офтальмологу? портал даст 751 — предупредим заранее
        if target.get('stop_if_same_specialty', True):
            existing = self.existing_specialty_record(
                target['lpu_id'], pid, slot.specialty_name)
            if existing:
                visit = (existing.get('visitStart') or '?').replace('T', ' ')
                self.notifier.error(
                    f'У тебя уже есть запись по этой специальности ({visit}). '
                    f'Портал вторую не даёт. Отменить старую можно на портале, '
                    f'потом снова `hunt`.'
                )
                return 'exists'

        # подтверждение в интерактивном режиме — карточкой, не JSON-ом
        if interactive and not consent:
            print('\n' + self.card(target, slot, include_patient=True) + '\n')
            answer = input('Записывать на этот слот? [y/N] ').strip().lower()
            if answer not in ('y', 'yes', 'д', 'да'):
                self.notifier.info('Отмена — слот пропущен.')
                return 'declined'

        body = {
            'esiaId': None,
            'lpuId': str(target['lpu_id']),
            'patientId': pid,
            'appointmentId': slot.id,          # строго id слота из appointments
            'referralId': None,
            'ipmpiCardId': None,
            'recipientEmail': self.profile.get('email') or None,
            'patientLastName': self.profile['last_name'].strip(),
            'patientFirstName': self.profile['first_name'].strip(),
            'patientMiddleName': (self.profile.get('middle_name') or '').strip(),
            'patientBirthdate': birthdate_iso(self.profile['birthdate']),
            'room': slot.room,
            'num': slot.number,
            'address': slot.address,
            'visitDate': slot.visit_start,
        }
        result = self._create_with_retries(target, slot, body)
        return result

    def _create_with_retries(self, target: dict, slot: Slot, body: dict) -> str:
        attempts, max_attempts = 0, 3
        while attempts < max_attempts:
            if self.should_stop is not None and self.should_stop():
                return 'declined'
            attempts += 1
            try:
                self.client.appointment_create(body)
                return self._verify_after_create(target, slot, body)
            except AttachmentDenied:
                raise
            except GorzdravError as e:
                if e.code == 615 and attempts < max_attempts:
                    log.warning('errorCode 615 (пиковая ошибка портала), '
                                'повтор %s/%s через 3с…', attempts, max_attempts)
                    time.sleep(3)
                    continue
                if e.code == 1 and attempts == 1:
                    # каноническое тело не приняли — пробуем минимальное из трёх
                    # полей (подход клиента egorantonov, живьём не проверен)
                    log.warning('errorCode 1 на create — пробую минимальное тело.')
                    body = {k: body[k] for k in ('lpuId', 'patientId', 'appointmentId')}
                    continue
                if e.code == 751:
                    self.notifier.error(
                        'Портал не дал записать: уже есть активная запись по '
                        'этой специальности (751). Проверь записи на портале.'
                    )
                    return 'exists'
                if e.code == 645:
                    log.info('Слот исчез (645) — расписание переиграло.')
                    return 'vanished'
                if e.transient and attempts < max_attempts:
                    log.warning('Временная ошибка create (%s), повтор через 3с…',
                                e.friendly)
                    time.sleep(3)
                    continue
                self.notifier.error(
                    f'Запись не прошла: {e.friendly} (errorCode {e.code})'
                    + (f', requestId {e.request_id}' if e.request_id else ''))
                return 'transient' if e.transient else 'error'
        return 'error'

    def _verify_after_create(self, target: dict, slot: Slot, body: dict) -> str:
        """create вернул успех — но записью это считается только после контроля."""
        try:
            records = self.client.patient_appointments(target['lpu_id'],
                                                       body['patientId'])
        except GorzdravError as e:
            self.notifier.broadcast(
                f'Портал принял запись, но проверить список записей не удалось '
                f'({e.friendly}). Проверь сам на портале, слот: '
                f'{slot.visit_start.replace("T", " ")}.',
                kind='WARN',
            )
            return 'unverified'
        ok = any(r.get('visitStart') == slot.visit_start for r in records)
        if ok:
            text = ('✅ ЗАПИСАН\n' + self.card(target, slot))
            self.notifier.broadcast(text, kind='OK')
            self.notifier.desktop_notify('Запись оформлена', slot.visit_start)
            phone = (self.profile.get('phone') or '').strip()
            if phone:
                try:
                    self.client.patient_update_phone(target['lpu_id'], body['patientId'],
                                                     phone)
                except GorzdravError as e:
                    self.notifier.warn(
                        'Телефон в медкарту обновить не удалось — запись от этого '
                        'не меняется.')
        else:
            self.notifier.broadcast(
                'Портал ответил успехом, но в списке записей слота не вижу — '
                'запись НЕ подтверждена. Проверь на портале и попробуй снова.',
                kind='ERR',
            )
            return 'unverified'
        return 'booked'
