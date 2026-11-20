"""Предстоящие талоны Горздрава и экспорт отдельного приёма в календарь."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json

from .client import GorzdravError, ORIGIN
from .config import profile_is_ready
from .desktop_catalog import clinic_patient_id
from .desktop_plan import patient_name


VISITS_URL = ORIGIN + '/lk-visits'
CANCEL_URL = ORIGIN + '/service-cancel'
PETERSBURG = timezone(timedelta(hours=3))


@dataclass(frozen=True)
class Visit:
    scope: tuple[str, str]
    patient_id: str
    appointment_id: str
    start: datetime
    end: datetime | None
    patient: str
    clinic: str
    address: str
    specialty: str
    doctor: str
    room: str
    referral: bool

    @property
    def key(self):
        identity = (*self.scope, self.patient_id, self.appointment_id, self.start.isoformat())
        return sha256(json.dumps(identity).encode()).hexdigest()

    @property
    def title(self):
        return self.specialty or self.doctor or 'Приём у врача'

    @property
    def place(self):
        return ' · '.join(filter(None, (self.address, f'каб. {self.room}' if self.room else '')))


def visit_scopes(plan):
    """Пациент и его поликлиника из сохранённых поисков или кэша медкарт."""
    patients = {key: profile for key, profile in plan['patients'].items() if profile_is_ready(profile)}
    clinics = {(key, str(clinic)): '' for key, profile in patients.items()
               for clinic in (profile.get('patient_ids') or {})}
    for job in plan['jobs'].values():
        if job.get('patient') in patients:
            target = job['target']
            clinics[job['patient'], str(target['lpu_id'])] = target.get('lpu_name') or ''
    return {scope: (patients[scope[0]], clinic) for scope, clinic in clinics.items()}


def _time(value):
    if not isinstance(value, str) or 'T' not in value:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp.astimezone(PETERSBURG) if stamp.tzinfo else stamp.replace(tzinfo=PETERSBURG)


def _active_records(client, clinic_id, patient_id):
    records = client.patient_appointments(int(clinic_id), patient_id)
    if any(str(row.get('lpuId')) != clinic_id or
           (row.get('patientId') is not None and str(row['patientId']) != patient_id) for row in records):
        raise GorzdravError(-1, 'Горздрав вернул талоны вне выбранной поликлиники или пациента.')
    return records


def _read_visits(client, scope, profile, clinic):
    patient_id = clinic_patient_id(client, scope[1], profile)
    if not patient_id:
        return []
    visits = []
    now = datetime.now(PETERSBURG)
    for row in _active_records(client, scope[1], patient_id):
        start = _time(row.get('visitStart'))
        if not row.get('appointmentId') or start is None or start <= now:
            continue
        doctor = row.get('doctorRendingConsultation') or {}
        specialty = row.get('specialityRendingConsultation') or {}
        visits.append(Visit(
            scope, patient_id, str(row['appointmentId']), start, _time(row.get('visitEnd')),
            patient_name(profile), row.get('lpuShortName') or clinic or row.get('lpuFullName') or f'ЛПУ № {scope[1]}',
            row.get('address') or row.get('lpuAddress') or '', specialty.get('name') or '',
            doctor.get('name') or '', str(row.get('room') or ''),
            bool(row.get('isAppointmentByReferral') or row.get('referralId')),
        ))
    return visits


def fetch_visits(client, scopes):
    results = {}
    with client.s:
        for scope, (profile, clinic) in scopes.items():
            try:
                results[scope] = _read_visits(client, scope, profile, clinic)
            except (GorzdravError, ValueError):
                results[scope] = None
    return results


def cancel_visit(client, visit, profile):
    """Перед единственным POST сверяем талон, после проверяем его исчезновение."""
    with client.s:
        current = next((row for row in _read_visits(client, visit.scope, profile, visit.clinic)
                        if row.key == visit.key), None)
        if current is None:
            return 'absent'
        if current.referral:
            return 'referral'
        accepted = False
        try:
            accepted = client.appointment_cancel({
                'appointmentId': current.appointment_id, 'lpuId': current.scope[1],
                'patientId': current.patient_id, 'esiaId': None,
            })
        except GorzdravError:
            pass  # При сетевой ошибке запрос мог дойти до Горздрава.
        try:
            records = _active_records(client, current.scope[1], current.patient_id)
        except GorzdravError:
            return 'unverified'
        if any(str(row.get('appointmentId')) == current.appointment_id for row in records):
            return 'unverified'
        return 'cancelled' if accepted else 'absent'


def _ical_text(value):
    return (value.replace('\\', '\\\\').replace('\r\n', '\n').replace('\r', '\n')
            .replace('\n', r'\n').replace(';', r'\;').replace(',', r'\,'))


def _ical_line(line):
    """Перенос на 75 октетах, сохраняя целые символы UTF-8."""
    parts = [bytearray()]
    for character in line:
        chunk = character.encode('utf-8')
        if len(parts[-1]) + len(chunk) > 75:
            parts.append(bytearray(b' '))
        parts[-1].extend(chunk)
    return b'\r\n'.join(parts)


def calendar_event(visit):
    def utc(value):
        return value.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')

    lines = [
        'BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Gorzdrav Slot Hunter//Visits//RU',
        'BEGIN:VEVENT', f'UID:{visit.key}@gorzdrav-slot-hunter',
        f'DTSTAMP:{utc(datetime.now(timezone.utc))}', f'DTSTART:{utc(visit.start)}',
    ]
    if visit.end and visit.end > visit.start:
        lines.append(f'DTEND:{utc(visit.end)}')
    location = ' · '.join(filter(None, (visit.clinic, visit.place)))
    details = visit.doctor if visit.doctor != visit.title else ''
    lines.extend([
        f'SUMMARY:{_ical_text(visit.title)}', f'LOCATION:{_ical_text(location)}',
        f'DESCRIPTION:{_ical_text(details)}', 'CLASS:PRIVATE', f'URL:{VISITS_URL}',
        'BEGIN:VALARM', 'ACTION:DISPLAY', 'TRIGGER:-PT1H',
        'DESCRIPTION:Приём у врача через час', 'END:VALARM', 'END:VEVENT', 'END:VCALENDAR',
    ])
    return b'\r\n'.join(map(_ical_line, lines)) + b'\r\n'
