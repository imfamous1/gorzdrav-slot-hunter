"""Замена сборки из отдельного процесса с восстановлением фонового поиска."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from .config import CONFIG_PATH, _read_json, _write_json, load_config, load_state
from .desktop_plan import read_plan, task_states
from .desktop_update import WORK_PREFIX, UpdateError, bundle_binary, installed_bundle
from .desktop_worker import ACTIVE_PHASES, FINISHED_JOBS, STOP, _control_update, _launch_worker, read_status
from .paths import data_path
from .process_lock import is_locked, process_lock


@dataclass(frozen=True)
class PausedSearch:
    run_id: str
    stop_marker: dict | None


def _wait_for(condition, timeout, message):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            raise UpdateError(message)
        time.sleep(0.25)


def _spawn(executable, *arguments):
    options = ({'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
               if sys.platform == 'win32' else {'start_new_session': True})
    return subprocess.Popen(
        [str(executable), *map(str, arguments)], cwd=data_path(),
        env={**os.environ, 'PYINSTALLER_RESET_ENVIRONMENT': '1'},
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **options,
    )


def start_installer(work, bundle):
    gate = process_lock(work / 'window.lock')
    gate.__enter__()
    helper = None
    try:
        helper = _spawn(bundle_binary(work / 'helper' / bundle.name), '--install-update', work)

        def ready():
            if helper.poll() is not None:
                raise UpdateError('Не удалось запустить установщик обновления.')
            return (work / 'installer-ready').exists()

        _wait_for(ready, 20, 'Установщик не ответил. Попробуй ещё раз.')
    except BaseException:
        try:
            if helper is not None and helper.poll() is None:
                helper.terminate()
                helper.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            gate.__exit__(None, None, None)
            shutil.rmtree(work, ignore_errors=True)
        raise
    return gate


def _pause_search(executable):
    with _control_update():
        status = read_status()
        if status['phase'] not in ACTIVE_PHASES:
            return None
        if status['phase'] == 'stopping' or not status.get('per_job'):
            raise UpdateError('Дождись завершения фонового поиска и начни установку снова.')
        paused = PausedSearch(status['id'], _read_json(STOP))
        _write_json(STOP, {'id': paused.run_id})
    try:
        _wait_for(lambda: read_status()['phase'] not in ACTIVE_PHASES, 180,
                  'Фоновый поиск ещё ждёт ответ Горздрава. Попробуй обновить позже.')
    except UpdateError:
        _resume_search(paused, executable)
        raise
    return paused


def _resume_search(paused, executable):
    if paused is None:
        return
    with _control_update():
        status = read_status()
        if status['id'] != paused.run_id:
            raise UpdateError('Во время установки запустился другой фоновый поиск.')
        if _read_json(STOP, default={}).get('id') == paused.run_id:
            if paused.stop_marker is None:
                STOP.unlink()
            else:
                _write_json(STOP, paused.stop_marker)
        if status['phase'] in ACTIVE_PHASES:
            return
        state = load_state()
        plan = read_plan(_read_json(CONFIG_PATH), None)
        states = task_states(state, plan)
        remaining = any(
            job.get('run', {}).get('worker') == paused.run_id and not job['run'].get('stop')
            and states[key].get('phase') not in FINISHED_JOBS
            for key, job in plan['jobs'].items()
        )
        if remaining:
            last_scan = state.get('last_scan')
            resume_at = (datetime.fromisoformat(last_scan).timestamp() + float(load_config()['poll_interval_sec'])
                         if last_scan else None)
            _launch_worker(paused.run_id, executable=executable, per_job=True, resume_at=resume_at)
            return True


def _search_ready(paused):
    status = read_status()
    if status.get('id') != paused.run_id or status['phase'] in ('failed', 'interrupted'):
        raise UpdateError('Фоновый поиск не удалось возобновить.')
    return (status['phase'] == 'running' and status.get('live_plan')) or status['phase'] in ('booked', 'stopped')


def _move_bundle(source, destination):
    # Windows can hold the executable for a moment after the process releases its lock.
    for attempt in range(20):
        try:
            source.rename(destination)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.25)


def replace_bundle(work, target):
    """Вернуть прежнюю сборку, если замена каталога приложения не удалась."""
    backup = work / 'previous'
    _move_bundle(target, backup)
    try:
        _move_bundle(work / 'candidate', target)
    except OSError:
        _move_bundle(backup, target)
        raise


def _install(work, target):
    _wait_for(lambda: not is_locked(work / 'window.lock'), 30,
              'Окно приложения не закрылось. Попробуй обновить ещё раз.')
    executable = bundle_binary(target)
    paused = _pause_search(executable)
    replaced = False
    try:
        with _control_update():
            if read_status()['phase'] in ACTIVE_PHASES:
                raise UpdateError('Во время установки запустился фоновый поиск. Попробуй ещё раз.')
            replace_bundle(work, target)
            replaced = True
        if _resume_search(paused, executable):
            _wait_for(lambda: _search_ready(paused), 35, 'Фоновый поиск ещё запускается.')
    except (OSError, ValueError, KeyError, UpdateError):
        if replaced:
            with _control_update():
                if read_status()['phase'] not in ACTIVE_PHASES:
                    _move_bundle(target, work / 'candidate')
                    _move_bundle(work / 'previous', target)
        _resume_search(paused, executable)
        raise


def run_installer(work):
    work = work.resolve()
    helper = installed_bundle()
    if not work.name.startswith(WORK_PREFIX) or helper.parent != work / 'helper':
        return 1
    target = work.parent / helper.name
    if not bundle_binary(target).is_file() or not bundle_binary(work / 'candidate').is_file():
        return 1

    with process_lock(data_path() / '.cache' / 'desktop-update.lock'), process_lock(work / 'installer.lock'):
        (work / 'installer-ready').touch()
        try:
            _install(work, target)
        except (OSError, UpdateError, ValueError, KeyError) as error:
            message = (str(error) if isinstance(error, UpdateError)
                       else 'Не удалось завершить установку. Проверь статус фонового поиска.')
            _write_json(work / 'result.json', {'error': message})
        else:
            _write_json(work / 'result.json', {})
        _spawn(bundle_binary(target), '--update-finished', work)
    return 0


def finish_install(work):
    if (not work.name.startswith(WORK_PREFIX) or work.parent != installed_bundle().parent
            or not (work / 'result.json').is_file()):
        raise UpdateError('Не удалось прочитать результат установки.')
    result = _read_json(work / 'result.json')
    _wait_for(lambda: not is_locked(work / 'installer.lock'), 20, 'Установщик ещё завершает работу.')
    # The helper itself is in the staging directory and must exit before cleanup on Windows.
    for attempt in range(20):
        try:
            shutil.rmtree(work)
        except PermissionError:
            if attempt == 19:
                break
            time.sleep(0.25)
        else:
            break
    return result.get('error', '')
