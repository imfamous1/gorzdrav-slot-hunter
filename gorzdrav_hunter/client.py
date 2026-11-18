"""Клиент публичного API горздрава СПб (https://gorzdrav.spb.ru/_api/api/v2).

Правила, зашитые здесь (по живой документации cucumberian/spb-gorzdrav-skill):
- ошибки приходят с HTTP 200 и success:false — смотрим errorCode, а не статус;
- токен сессии приходит заголовком ``token`` и отправляется обратно;
- между любыми запросами держим паузу — вежливость к городскому API;
- appointmentId никогда не генерируем — берём строго ``id`` слота из
  appointments этого ЛПУ и этого врача (формат зависит от МИС поликлиники).
"""
from __future__ import annotations

import json
import logging
import socket
import sys
import time
import urllib.parse
from dataclasses import dataclass

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection

log = logging.getLogger(__name__)

ORIGIN = 'https://gorzdrav.spb.ru'
BASE = f'{ORIGIN}/_api/api/v2'
PORTAL = f'{ORIGIN}/service-free-schedule'

# коды, при которых операция временно недоступна и ретрай уместен
TRANSIENT_CODES = {602, 603, 615, 616, 660}

# человекочитаемые формулировки для частых кодов
FRIENDLY = {
    1: 'внутренняя ошибка сервиса портала',
    8: 'не удалось найти прикрепление по полису ОМС',
    37: 'в этой поликлинике нет онлайн-записи по этой специальности',
    38: 'врачей этой специальности сейчас нет в расписании',
    39: 'свободных талонов нет',
    49: 'медорганизация не найдена',
    602: 'медорганизация не отвечает',
    603: 'таймаут ответа медорганизации',
    610: 'не удалось получить данные о медорганизации',
    615: 'внутренняя ошибка сервиса (бывает пиками), нужен повтор',
    616: 'ошибка МИС медорганизации',
    620: 'карточка пациента не найдена в этой поликлинике',
    645: 'расписание врача недоступно (слот исчез)',
    660: 'портал просит повторить позже',
    751: 'уже есть активная запись к этому специалисту',
}


class GorzdravError(Exception):
    """Ошибка API: HTTP 200 + success:false, либо транспортный сбой (code<0)."""

    def __init__(self, code: int, message: str = '', request_id: str | None = None):
        self.code = code
        self.message = message
        self.request_id = request_id
        super().__init__(f'errorCode={code}: {message}')

    @property
    def transient(self) -> bool:
        return self.code in TRANSIENT_CODES or self.code < 0

    @property
    def friendly(self) -> str:
        return FRIENDLY.get(self.code, self.message or f'ошибка портала {self.code}')


@dataclass
class Slot:
    lpu_id: int
    specialty_id: str
    specialty_name: str
    doctor_id: str
    doctor_name: str
    id: str            # appointmentId — строго как отдал API
    visit_start: str   # ISO 'YYYY-MM-DDTHH:MM:SS'
    visit_end: str
    room: str | None
    number: str | None
    address: str | None

    @property
    def date(self) -> str:
        return self.visit_start[:10]


class _MacOSDirectAdapter(HTTPAdapter):
    """Привязывает IPv4-соединения к сетевому интерфейсу macOS."""

    def __init__(self, interface: str):
        self.interface_index = socket.if_nametoindex(interface)
        super().__init__()

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        # IP_BOUND_IF из Darwin <netinet/in.h> отсутствует в socket у Python 3.11.
        ip_bound_if = 25
        pool_kwargs['socket_options'] = [
            *HTTPConnection.default_socket_options,
            (socket.IPPROTO_IP, ip_bound_if, self.interface_index),
        ]
        return super().init_poolmanager(connections, maxsize, block, **pool_kwargs)


class GorzdravClient:
    """Сессия с токеном и вежливыми паузами между запросами."""

    def __init__(
        self,
        verify: str | bool = True,
        timeout: float = 30.0,
        sleep_between: float = 1.0,
        user_agent: str = 'gorzdrav-slot-hunter',
        *,
        direct_interface: str | None = None,
    ):
        self.s = requests.Session()
        if direct_interface:
            if sys.platform != 'darwin':
                raise ValueError('http.direct_interface поддерживается только на macOS')
            self.s.trust_env = False
            self.s.mount(f'{ORIGIN}/', _MacOSDirectAdapter(direct_interface))
        self.s.headers.update({'User-Agent': user_agent})
        self.s.verify = verify
        self.timeout = timeout
        self.sleep_between = max(1.0, sleep_between)
        self._token: str | None = None
        self._last_call = 0.0

    # ------------------------------------------------------------------ core

    def _pace(self) -> None:
        if self.sleep_between <= 0:
            return
        wait = self.sleep_between - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)

    def _call(
        self,
        method: str,
        path: str,
        *,
        query: str | None = None,
        json_body: dict | None = None,
    ):
        url = f'{BASE}{path}'
        if query:
            url += '?' + query
        self._pace()
        try:
            resp = self.s.request(
                method, url, json=json_body, timeout=self.timeout, allow_redirects=False,
            )
        except requests.RequestException as e:
            raise GorzdravError(-1, f'сетевая ошибка: {e.__class__.__name__}') from e
        finally:
            self._last_call = time.monotonic()

        token = resp.headers.get('token')
        if token:
            self._token = token
            self.s.headers['token'] = token

        if not 200 <= resp.status_code < 300:
            raise GorzdravError(-1, f'HTTP {resp.status_code} от {path}')
        try:
            body = resp.json()
        except ValueError as e:
            raise GorzdravError(-1, f'не-JSON ответ от {path}') from e

        if not isinstance(body, dict):
            raise GorzdravError(-1, f'неожиданный ответ API от {path}')
        if not body.get('success', False):
            raise GorzdravError(
                int(body.get('errorCode') or -1),
                body.get('message') or '',
                body.get('requestId'),
            )
        result = body.get('result')
        # у списковых эндпоинтов result — массив; иной мусор трактуем как пусто
        return result

    # -------------------------------------------------------- справочники

    def districts(self) -> list[dict]:
        return self._call('GET', '/shared/districts') or []

    def lpus(self, district_id: int | None = None) -> list[dict]:
        if district_id is None:
            return self._call('GET', '/shared/lpus') or []
        return self._call('GET', f'/shared/district/{district_id}/lpus') or []

    def specialties(self, lpu_id: int) -> list[dict]:
        """Специальности ЛПУ. errorCode 37 («нет записи») → пусто."""
        try:
            return self._call('GET', f'/schedule/lpu/{lpu_id}/specialties') or []
        except GorzdravError as e:
            if e.code == 37:
                return []
            raise

    def doctors(self, lpu_id: int, specialty_id: str) -> list[dict]:
        """Врачи специальности. errorCode 38 («нет врачей») → пусто."""
        try:
            return self._call(
                'GET',
                f'/schedule/lpu/{lpu_id}/speciality/{specialty_id}/doctors',
            ) or []
        except GorzdravError as e:
            if e.code == 38:
                return []
            raise

    def appointments(self, lpu_id: int, doctor_id: str) -> list[dict]:
        """Свободные слоты врача. errorCode 39 («талонов нет») → пусто."""
        try:
            return self._call(
                'GET', f'/schedule/lpu/{lpu_id}/doctor/{doctor_id}/appointments'
            ) or []
        except GorzdravError as e:
            if e.code == 39:
                return []
            raise

    # -------------------------------------------------------- запись (ПДн!)

    def patient_search(
        self,
        lpu_id: int,
        last_name: str,
        first_name: str,
        middle_name: str = '',
        birthdate_iso: str = '',
        birthdate_dmy: str = '',
    ) -> str | None:
        """id медкарты пациента в данном ЛПУ; None — карточка не найдена (620).

        Кириллицу кодируем urllib.parse.quote (пробел = %20, не '+').
        """
        q = {
            'lpuId': str(lpu_id),
            'lastName': last_name,
            'firstName': first_name,
            'middleName': middle_name or '',
            'birthdate': birthdate_iso,
            'birthdateValue': birthdate_dmy,
        }
        query = urllib.parse.urlencode(q, quote_via=urllib.parse.quote, safe='')
        try:
            result = self._call('GET', '/patient/search', query=query)
        except GorzdravError as e:
            if e.code == 620:
                return None
            raise
        return str(result) if result is not None else None

    def patient_appointments(self, lpu_id: int, patient_id: str) -> list[dict]:
        """Активные записи пациента в этом ЛПУ."""
        query = urllib.parse.urlencode(
            {'lpuId': str(lpu_id), 'patientId': str(patient_id)},
            quote_via=urllib.parse.quote, safe='',
        )
        return self._call('GET', '/appointments', query=query) or []

    def appointment_create(self, body: dict) -> None:
        """Создать запись. Успех = success:true с пустым result (не финал!)."""
        self._call('POST', '/appointment/create', json_body=body)

    def appointment_cancel(self, body: dict) -> None:
        self._call('POST', '/appointment/cancel', json_body=body)

    def appointment_change(
        self, *, lpu_id: int, patient_id: str, previous_id: str, slot_id: str,
    ) -> None:
        """Штатный перенос с портала, обе записи передаются в одном запросе."""
        self._call('POST', '/appointment/change', json_body={
            'esiaId': None,
            'appointmentId': slot_id,
            'previousAppointmentId': previous_id,
            'lpuId': str(lpu_id),
            'patientId': patient_id,
            'referralId': None,
        })

    def patient_update_phone(self, lpu_id: int, patient_id: str, phone10: str) -> None:
        body = {
            'lpuId': str(lpu_id),
            'patientId': str(patient_id),
            'mobilePhoneNumber': phone10,
        }
        self._call('POST', '/patient/update', json_body=body)


def portal_link(
    district_id: int | None,
    lpu_id: int,
    speciality_id: str | None,
    doctor_id: str | None,
) -> str:
    """Глубокая ссылка на сервис «Свободное расписание» портала.

    Фрагмент после # — URL-кодированный JSON-массив одноключевых объектов.
    Автоподстановка фильтров порталом не гарантирована, но ссылка корректная.
    """
    pairs = [
        ('district', district_id),
        ('lpu', lpu_id),
        ('speciality', speciality_id),
        ('schedule', doctor_id),
        ('doctor', doctor_id),
    ]
    frag = json.dumps(
        [{k: str(v)} for k, v in pairs if v not in (None, '')],
        ensure_ascii=False,
    )
    return f'{PORTAL}#{urllib.parse.quote(frag, safe="")}'
