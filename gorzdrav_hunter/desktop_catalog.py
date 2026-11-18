"""Каталоги и чтение действующих записей для формы поиска."""
from datetime import date, datetime
import re

from .config import birthdate_iso


CATALOG = ('district', 'clinic', 'specialty', 'doctor')
ANY_DOCTOR = {'id': '', 'name': 'Любой врач'}


def _words(text):
    return re.findall(r'[^\W_]+', text.casefold().replace('ё', 'е'))


def match_choices(choices, query):
    terms = _words(query)
    if not terms:
        return choices
    matches = []
    for option in choices:
        text = ' '.join(_words(option.get('label') or option['name']))
        if all(word in text for word in terms):
            matches.append(option)
    return matches


def visit_label(visit_start):
    return datetime.fromisoformat(visit_start).strftime('%d.%m.%Y · %H:%M')


def fetch_catalog(client, level, choices):
    with client.s:
        if level == 'district':
            records = client.districts()
        elif level == 'clinic':
            records = [
                {'id': row['id'], 'name': row['lpuShortName'], 'phone': row.get('phone'),
                 'label': ' — '.join(filter(None, (row['lpuShortName'], row.get('address'))))}
                for row in client.lpus(int(choices['district']['id']))
                if row.get('isActive') and row.get('lpuShortName')
            ]
        elif level == 'specialty':
            records = client.specialties(int(choices['clinic']['id']))
        else:
            records = client.doctors(int(choices['clinic']['id']), choices['specialty']['id'])
    options = [row if level == 'clinic' else {'id': row['id'], 'name': row['name']}
               for row in records if row.get('name')]
    options.sort(key=lambda row: row['name'].casefold())
    return [ANY_DOCTOR, *options] if level == 'doctor' else options


def fetch_appointments(client, clinic_id, spec_id, profile):
    with client.s:
        patient = (profile.get('patient_ids') or {}).get(clinic_id)
        if not patient:
            patient = client.patient_search(
                int(clinic_id), profile['last_name'], profile['first_name'], profile.get('middle_name', ''),
                birthdate_iso(profile['birthdate']), profile['birthdate'],
            )
        if not patient:
            return []
        matches = []
        for record in client.patient_appointments(int(clinic_id), patient):
            visit = record.get('visitStart') or ''
            spec = record.get('specialityRendingConsultation') or {}
            if (not visit or not record.get('appointmentId') or record.get('referralId')
                    or record.get('isAppointmentByReferral')
                    or str(record.get('lpuId')) != clinic_id or str(spec.get('id')) != spec_id
                    or datetime.fromisoformat(visit).date() < date.today()):
                continue
            source = {'appointment_id': str(record['appointmentId']),
                      'specialty_id': spec_id, 'visit_start': visit}
            matches.append({'id': source['appointment_id'], 'name': visit_label(visit), 'source': source})
        return sorted(matches, key=lambda item: item['source']['visit_start'])
