"""Пациенты и независимые задачи настольной версии."""
from __future__ import annotations

from copy import deepcopy

FIRST_JOB = 'original'


def read_plan(config: dict, profile: dict | None) -> dict:
    if 'desktop_plan' in config:
        return deepcopy(config['desktop_plan'])
    patient_id = 'primary' if profile and profile.get('last_name') else None
    patients = {patient_id: profile} if patient_id else {}
    jobs = {}
    for index, target in enumerate(config.get('targets', [])):
        job_id = FIRST_JOB if index == 0 else f'original-{index + 1}'
        jobs[job_id] = {
            'target': deepcopy(target), 'patient': patient_id,
            'consent': bool(profile and profile.get('consent_booking') and target.get('auto_book')),
        }
    return {'patients': patients, 'jobs': jobs}


def apply_plan(config: dict, plan: dict) -> dict:
    return {
        **config, 'desktop_plan': deepcopy(plan),
        'targets': [deepcopy(job['target']) for job in plan['jobs'].values()],
    }


def patient_name(profile: dict) -> str:
    return ' '.join(filter(None, (profile.get('last_name'), profile.get('first_name'),
                                  profile.get('middle_name'))))


def task_title(job: dict, patients: dict) -> str:
    target = job['target']
    service = target.get('doctor_label') or target.get('specialty_label') or target.get('lpu_name')
    profile = patients.get(job.get('patient'), {})
    return ' · '.join(filter(None, (service, patient_name(profile))))


def transfer_key(job: dict) -> tuple | None:
    target = job['target']
    source = target.get('reschedule_from')
    if source:
        return str(target['lpu_id']), job.get('patient'), source['appointment_id']
    return None


def task_states(state: dict, plan: dict) -> dict:
    if 'desktop_jobs' not in state:
        state['desktop_jobs'] = {}
        if FIRST_JOB in plan['jobs']:
            state['desktop_jobs'][FIRST_JOB] = {
                key: deepcopy(state[key]) for key in (
                    'seen', 'booked_ids', 'booked', 'found_cache', 'found_cache_time',
                    'last_scan', 'paused', 'reschedules',
                ) if key in state
            }
    for job_id in plan['jobs']:
        job = state['desktop_jobs'].setdefault(job_id, {})
        revision = plan['jobs'][job_id].get('revision')
        if revision != job.get('revision'):
            job.clear()
            job['revision'] = revision
        for key, default in (
            ('seen', {}), ('booked_ids', []), ('booked', []), ('found_cache', []),
            ('last_scan', None), ('paused', {}), ('reschedules', {}),
        ):
            job.setdefault(key, default)
    return state['desktop_jobs']
