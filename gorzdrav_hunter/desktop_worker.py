"""Управление фоновым поиском и журнал настольного приложения."""
from __future__ import annotations

from datetime import datetime
from contextlib import ExitStack, contextmanager
import logging
from logging.handlers import RotatingFileHandler
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from .booker import Booker
from .client import GorzdravClient, GorzdravError
from .config import (
    CONFIG_PATH, LOG_DIR, ROOT, _read_json, _write_json, load_config,
    load_profile, load_state, normalize_target, save_profile, save_state,
)
from .desktop_plan import apply_plan, read_plan, task_states, transfer_key
from .hunter import Hunter
from .notify import Notifier
from .paths import RESOURCES
from .process_lock import is_locked, process_lock
from .tls import build_bundle

log = logging.getLogger(__name__)
STATUS = ROOT / 'search.json'
STOP = ROOT / 'stop.json'
EVENTS = ROOT / 'events.json'
CONTROL_LOCK = ROOT / '.cache' / 'desktop.lock'
WORKER_LOCK = ROOT / '.cache' / 'hunter.lock'
ACTIVE_PHASES = {'starting', 'running', 'stopping'}
FINISHED_JOBS = {'completed', 'closed'}


def read_status() -> dict:
    status = _read_json(STATUS, default={})
    if is_locked(WORKER_LOCK):
        phase = ('running' if status.get('phase') in ('starting', 'running')
                 and not _stop_requested(status.get('id')) else 'stopping')
    elif status.get('phase') == 'starting' and time.time() - status['started_at'] < 30:
        phase = 'stopping' if _stop_requested(status['id']) else 'starting'
    else:
        phase = status.get('phase', 'idle')
        if phase in ('starting', 'running'):
            phase = 'interrupted'
    return {**status, 'phase': phase}


def _stop_requested(run_id: str | None) -> bool:
    return bool(run_id) and _read_json(STOP, default={}).get('id') == run_id


def request_stop() -> None:
    with process_lock(CONTROL_LOCK):
        status = read_status()
        if status['phase'] in ('starting', 'running'):
            _write_json(STOP, {'id': status['id']})


def job_phase(status, job, state):
    if not job:
        return 'idle'
    if job.get('revision') != state.get('revision'):
        state = {}
    phase = state.get('phase')
    run = job.get('run', {})
    if phase in FINISHED_JOBS:
        return phase
    current = status['phase']
    if status.get('per_job'):
        if run.get('worker') != status['id']:
            return 'stopped' if run else 'idle'
        acknowledged = run['token'] == state.get('run_token')
        if acknowledged and phase == 'stopped':
            return 'stopped'
        if current in ACTIVE_PHASES:
            if run.get('stop') or current == 'stopping':
                return 'stopping'
            if acknowledged and phase != 'starting':
                return 'retry' if phase == 'retry' else 'running'
            return 'starting'
        return current if current in ('failed', 'interrupted') else 'stopped'
    if job.get('enabled', True) and current in ACTIVE_PHASES:
        return 'retry' if current == 'running' and phase == 'retry' else current
    return 'stopped' if phase == 'stopped' or run else 'idle'


def stop_job(job_id, run_id, *, token=None):
    with _control_update():
        current = read_status()
        raw = _read_json(CONFIG_PATH, default={})
        plan = read_plan(raw, load_profile())
        job = plan['jobs'].get(job_id)
        if current.get('id') != run_id or current['phase'] not in ('starting', 'running'):
            raise ValueError('Этот поиск уже завершился.')
        states = task_states(load_state(), plan)
        if job_phase(current, job, states.get(job_id, {})) not in ('starting', 'running', 'retry'):
            raise ValueError('Этот поиск уже завершился.')
        if not current.get('per_job'):
            active = [key for key, other in plan['jobs'].items()
                      if job_phase(current, other, states[key]) in ('starting', 'running', 'retry')]
            if active != [job_id]:
                raise ValueError('Эта версия фонового процесса поддерживает только общую остановку.')
            _write_json(STOP, {'id': current['id']})
            return
        if job['run']['token'] != token:
            raise ValueError('Этот поиск уже обновился.')
        job['run']['stop'] = True
        _write_json(CONFIG_PATH, apply_plan(raw, plan))


def start_desktop_job(job_id, job, profile):
    """Сохранить и запустить поисковую задачу выбранной вкладки."""
    with _control_update():
        status = read_status()
        previous_config = _read_json(CONFIG_PATH)
        config = previous_config or {}
        plan = read_plan(config, profile)
        prior = plan['jobs'].get(job_id)
        state = task_states(load_state(), plan).get(job_id, {})
        phase = job_phase(status, prior, state)
        if phase in ACTIVE_PHASES or phase == 'retry':
            raise ValueError('Дождись остановки этого поиска перед изменением настроек.')
        if any(item.get('status') == 'submitted' for item in state.get('reschedules', {}).values()):
            if not prior or prior.get('revision') != job.get('revision'):
                raise ValueError('Сначала дождись подтверждения текущего переноса.')
        if phase in FINISHED_JOBS and prior.get('revision') == job.get('revision'):
            raise ValueError('Измени параметры, чтобы начать новый поиск.')
        legacy_active = status['phase'] in ACTIVE_PHASES and not status.get('per_job')
        if legacy_active:
            raise ValueError('Дождись завершения поиска в прежнем фоновом процессе.')
        key = transfer_key(job)
        if key and any(other_id != job_id and transfer_key(other) == key
                       for other_id, other in plan['jobs'].items()):
            raise ValueError('Для этого приёма уже сохранён автоперенос.')
        normalize_target(job['target'], config)
        if status['phase'] == 'stopping':
            raise ValueError('Фоновый поиск завершается. Попробуй ещё раз.')
        launch = status['phase'] not in ACTIVE_PHASES
        run_id = uuid4().hex if launch else status.get('id')
        plan['jobs'][job_id] = {**job, 'run': {'worker': run_id, 'token': uuid4().hex}}
        config = apply_plan(config, plan)
        _write_json(CONFIG_PATH, config)
        if launch:
            try:
                _launch_worker(run_id, per_job=True)
            except OSError:
                if previous_config is None:
                    CONFIG_PATH.unlink(missing_ok=True)
                else:
                    _write_json(CONFIG_PATH, previous_config)
                raise
        return config


def append_live_plan(run_id: str, *, patient=None, job=None) -> dict:
    """Добавить новые записи, сохранив текущие задачи и обновлённые кэши."""
    with process_lock(CONTROL_LOCK):
        status = read_status()
        if status.get('id') != run_id or status['phase'] != 'running' or not status.get('live_plan'):
            raise ValueError('Фоновый поиск уже завершился или обновился.')
        config = _read_json(CONFIG_PATH)
        plan = read_plan(config, None)
        if patient is not None:
            patient_id, profile = patient
            if patient_id in plan['patients']:
                raise ValueError('Профиль уже сохранён.')
            plan['patients'][patient_id] = profile
        if job is not None:
            job_id, task = job
            if job_id in plan['jobs']:
                raise ValueError('Поиск уже сохранён.')
            key = transfer_key(task)
            if key and any(transfer_key(other) == key for other in plan['jobs'].values()):
                raise ValueError('Для этого приёма уже есть автоперенос.')
            normalize_target(task['target'], config)
            plan['jobs'][job_id] = task
        updated = apply_plan(config, plan)
        _write_json(CONFIG_PATH, updated)
        return updated


@contextmanager
def _control_update():
    for attempt in range(12):
        with ExitStack() as stack:
            try:
                stack.enter_context(process_lock(CONTROL_LOCK))
            except BlockingIOError:
                if attempt == 11:
                    raise
            else:
                yield
                return
        time.sleep(0.05)


def start_search(
    config: dict, profile: dict, *,
    imported_state: dict | None = None, executable: Path | None = None,
) -> None:
    with process_lock(CONTROL_LOCK):
        if read_status()['phase'] in ('starting', 'running', 'stopping'):
            raise ValueError('Поиск уже запущен.')
        _write_json(CONFIG_PATH, config)
        save_profile(profile)
        # Каждый явно запущенный поиск повторно рассматривает актуальные талоны.
        state = imported_state if imported_state is not None else load_state()
        if imported_state is None:
            for target in config['targets']:
                state['seen'].pop(str(target['lpu_id']), None)
                state['paused'].pop(str(target['lpu_id']), None)
            state['last_scan'] = None
        save_state(state)

        _launch_worker(uuid4().hex, executable=executable)


def _launch_worker(run_id, *, executable=None, per_job=False, resume_at=None):
    status = {'id': run_id, 'phase': 'starting', 'started_at': time.time(), 'per_job': per_job}
    if resume_at is not None:
        status['resume_at'] = resume_at
    _write_json(STATUS, status)
    args = [str(executable or sys.executable)]
    if executable is None and not getattr(sys, 'frozen', False):
        args.append(str(RESOURCES / 'desktop_app.py'))
    args.extend(('--worker', run_id))
    options = (
        {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
        if sys.platform == 'win32' else {'start_new_session': True}
    )
    try:
        subprocess.Popen(
            args, cwd=ROOT, env={**os.environ, 'PYINSTALLER_RESET_ENVIRONMENT': '1'},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **options,
        )
    except OSError:
        _write_json(STATUS, {**status, 'phase': 'failed'})
        raise


class DesktopNotifier(Notifier):
    def __init__(self, cfg, label=''):
        super().__init__(cfg)
        self.label = label

    def _print(self, kind: str, text: str) -> None:
        log.info('%s %s', kind, text)
        if kind in ('OK', 'WARN', 'ERR'):
            self._event(kind, text)

    def broadcast(self, text: str, kind: str = 'INFO') -> None:
        if self.label:
            text = self.label + '\n' + text
        if kind not in ('OK', 'WARN', 'ERR'):
            self._event(kind, text)
        super().broadcast(text, kind)

    @staticmethod
    def _event(kind: str, text: str) -> None:
        entries = _read_json(EVENTS, default=[])
        entries.append({
            'time': datetime.now().isoformat(timespec='seconds'),
            'kind': kind, 'text': text,
        })
        _write_json(EVENTS, entries[-100:])


class DesktopSearchGroup(Hunter):
    """Один цикл поиска с отдельной сессией и состоянием для каждой задачи."""

    def __init__(self, cfg, plan, state, client_factory, should_stop, *,
                 load_plan=None, finish=None, run_id=None):
        self.run_id = run_id
        active = [(key, job, normalize_target(job['target'], cfg))
                  for key, job in plan['jobs'].items() if self._requested(job)]
        if not active and run_id is None:
            raise ValueError('Включи хотя бы один поиск.')
        transfers = [key for _, job, _ in active if (key := transfer_key(job))]
        if len(transfers) != len(set(transfers)):
            raise ValueError('У одного приёма может быть только один автоперенос.')
        super().__init__(None, {**cfg, 'targets': [target for _, _, target in active]},
                         DesktopNotifier(cfg), None)
        self.should_stop = should_stop
        self.jobs = []
        self.tokens = {}
        self.state = state
        self.states = task_states(state, plan) if run_id else {}
        self.client_factory = client_factory
        self.load_plan = load_plan
        self.finish = finish
        self._extend(plan, active)

    def _requested(self, job):
        if self.run_id is None:
            return job.get('enabled', True)
        run = job.get('run', {})
        return run.get('worker') == self.run_id and not run.get('stop')

    def _job_stopped(self, job_id, token):
        if self.should_stop():
            return True
        if self.run_id is None:
            return False
        job = self.load_plan()['jobs'].get(job_id, {})
        return not self._requested(job) or job['run']['token'] != token

    def _extend(self, plan, active):
        self.states = task_states(self.state, {**plan, 'jobs': {key: job for key, job, _ in active}})
        profiles = plan['patients']
        def persist_task(_):
            save_state(self.state)

        for job_id, job, target in active:
            token = job.get('run', {}).get('token')
            self.tokens[job_id] = token
            if self.run_id:
                job_state = self.states[job_id]
                if job_state.get('phase') not in FINISHED_JOBS:
                    job_state.update(run_token=token, phase='starting')
            patient_id = job.get('patient')
            profile = dict(profiles.get(patient_id, {}))
            profile['patient_ids'] = dict(profile.get('patient_ids', {}))
            profile['consent_booking'] = bool(job.get('consent'))
            given = (profile.get('first_name') or '').strip()
            surname = (profile.get('last_name') or '').strip()
            label = f'{given} {surname[:1]}.' if given and surname else ''
            notifier = DesktopNotifier(self.cfg, label=label)
            client = self.client_factory()
            booker = Booker(
                client, profile, notifier,
                should_stop=lambda key=job_id, token=token: self._job_stopped(key, token),
                persist_profile=lambda updated, key=patient_id: self._update_cache(key, updated),
            )
            hunter = Hunter(client, {**self.cfg, 'targets': [target]}, notifier, booker,
                            persist_state=persist_task)
            self.jobs.append((job_id, hunter))

    def _load_additions(self):
        if self.load_plan is None:
            return False
        plan = self.load_plan()
        active = [(key, job, normalize_target(job['target'], self.cfg))
                  for key, job in plan['jobs'].items()
                  if key not in self.tokens and job.get('enabled', True)]
        if active:
            self._extend(plan, active)
        return bool(active)

    def _sync_jobs(self, *, persist):
        plan = self.load_plan()
        requested = {key: job for key, job in plan['jobs'].items() if self._requested(job)}
        attached = []
        for key, hunter in self.jobs:
            if key in requested and requested[key]['run']['token'] == self.tokens[key]:
                attached.append((key, hunter))
            else:
                hunter.client.s.close()
                self.tokens.pop(key)
        self.jobs = attached

        changed = False
        for key, job in plan['jobs'].items():
            run = job.get('run', {})
            if run.get('worker') != self.run_id or not run.get('stop'):
                continue
            stats = self.states.get(key, {})
            if ((stats.get('run_token') == run['token'] and stats.get('phase') == 'stopped')
                    or stats.get('phase') in FINISHED_JOBS):
                continue
            task_states(self.state, {**plan, 'jobs': {key: job}})[key].update(
                run_token=run['token'], phase='stopped',
            )
            changed = True
        new = [(key, job, normalize_target(job['target'], self.cfg))
               for key, job in requested.items() if key not in self.tokens]
        if new:
            self._extend(plan, new)
        if persist and (changed or new):
            save_state(self.state)

    def _done(self):
        return all(self.states[key].get('phase') in FINISHED_JOBS for key, _ in self.jobs)

    def settle_jobs(self, *, persist=True):
        """Принять остановки между опросами, сохранив интервал запросов к порталу."""
        self._sync_jobs(persist=persist)
        if not self._done():
            return False
        with _control_update():
            self._sync_jobs(persist=persist)
            if not self._done():
                return False
            if self.finish is not None:
                self.finish()
        return True

    @staticmethod
    def _update_cache(patient_id, profile):
        with _control_update():
            raw = _read_json(CONFIG_PATH, default={})
            if 'desktop_plan' in raw:
                saved = raw['desktop_plan']['patients'].get(patient_id)
                if saved is not None:
                    saved['patient_ids'] = {**(saved.get('patient_ids') or {}),
                                            **(profile.get('patient_ids') or {})}
                    _write_json(CONFIG_PATH, raw)
            else:
                save_profile(profile)

    def scan_once(self, state, *, persist=True, notify=True, dry_run=False, interactive=False):
        if self.run_id:
            if self.settle_jobs(persist=persist):
                return True
        else:
            self._load_additions()
        last_error = None
        scanned = False
        wait = False
        for job_id, hunter in self.jobs:
            job_state = self.states[job_id]
            if self.should_stop():
                return False
            if job_state.get('phase') in FINISHED_JOBS:
                continue
            if wait:
                time.sleep(max(1.0, float(self.cfg['http']['sleep_between_requests_sec'])))
            wait = True
            try:
                if self.should_stop():
                    return False
                if self._job_stopped(job_id, self.tokens.get(job_id)):
                    continue
                before = len(job_state['booked'])
                stopped = hunter.scan_once(job_state, persist=persist, notify=notify, dry_run=dry_run,
                                           interactive=interactive)
            except GorzdravError as error:
                last_error = error
                job_state['phase'] = 'retry'
                continue
            scanned = True
            booked = len(job_state['booked']) > before
            source = hunter.cfg['targets'][0].get('reschedule_from')
            if source:
                booked = job_state['reschedules'].get(source['appointment_id'], {}).get('status') == 'completed'
            job_state['phase'] = ('completed' if booked else 'closed') if stopped else 'watching'
            state['last_scan'] = job_state['last_scan']
        if persist:
            save_state(state)
        if self.run_id and self.settle_jobs(persist=persist):
            return True
        if last_error and not scanned:
            raise last_error
        if self.run_id:
            return False
        done = self._done()
        if done and self.finish is not None:
            with _control_update():
                if self._load_additions():
                    return False
                self.finish()
        return done


def run_worker(run_id: str) -> int:
    try:
        with process_lock(WORKER_LOCK):
            status = _read_json(STATUS, default={})
            if status.get('id') != run_id or status.get('phase') != 'starting':
                return 1
            if _stop_requested(run_id):
                _write_json(STATUS, {**status, 'phase': 'stopped'})
                return 0
            _write_json(STATUS, {**status, 'phase': 'running'})
            LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
            handler = RotatingFileHandler(
                LOG_DIR / 'desktop.log', maxBytes=300_000, backupCount=2, encoding='utf-8',
            )
            logging.basicConfig(level=logging.INFO, handlers=[handler],
                                format='%(asctime)s %(levelname)s %(message)s')
            logging.getLogger('urllib3').setLevel(logging.WARNING)
            try:
                cfg = load_config()
                verify = build_bundle()

                def make_client():
                    return GorzdravClient(
                        verify=verify, timeout=float(cfg['http']['timeout_sec']),
                        sleep_between=float(cfg['http']['sleep_between_requests_sec']),
                        direct_interface=cfg['http'].get('direct_interface'),
                    )

                should_stop = lambda: _stop_requested(run_id)
                state = load_state()
                raw = _read_json(CONFIG_PATH)
                if 'desktop_plan' in raw:
                    plan = read_plan(raw, load_profile())
                    status = {**status, 'live_plan': True}
                    _write_json(STATUS, {**status, 'phase': 'running'})
                    search = DesktopSearchGroup(
                        cfg, plan, state, make_client, should_stop,
                        load_plan=lambda: read_plan(_read_json(CONFIG_PATH), None),
                        finish=lambda: _write_json(STATUS, {**status, 'phase': 'finishing'}),
                        run_id=run_id if status.get('per_job') else None,
                    )
                    while time.time() < status.get('resume_at', 0) and not should_stop():
                        time.sleep(1)
                    code = search.run(
                        state, should_stop=(lambda: should_stop() or search.settle_jobs())
                        if search.run_id else should_stop,
                    )
                    booked = any(search.states[key].get('phase') == 'completed'
                                 for key, _ in search.jobs)
                else:
                    client = make_client()
                    notifier = DesktopNotifier(cfg)
                    booker = Booker(client, load_profile(), notifier, should_stop=should_stop)
                    before = len(state['booked'])
                    code = Hunter(client, cfg, notifier, booker).run(state, should_stop=should_stop)
                    booked = len(state['booked']) > before
                phase = 'booked' if booked else 'stopped'
                _write_json(STATUS, {**status, 'phase': phase})
                return code
            except Exception:
                log.exception('Фоновый поиск завершился с ошибкой.')
                _write_json(STATUS, {**status, 'phase': 'failed'})
                return 1
    except BlockingIOError:
        return 1
