"""Контроллер настольного интерфейса Qt Quick."""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from queue import SimpleQueue
import re
import sys
import threading
from uuid import uuid4

from PySide6.QtCore import QObject, Property, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtQuickControls2 import QQuickStyle

from . import __version__
from .client import ATTACHMENT_CHECK_URL, GorzdravClient, GorzdravError
from .config import (
    CONFIG_PATH, ROOT, _read_json, _write_json, format_time_window, load_profile, load_state,
    parse_book_time, profile_is_ready,
)
from .desktop_catalog import (
    ANY_DOCTOR, CATALOG, clinic_patient_id, fetch_appointments, fetch_catalog, match_choices, visit_label,
)
from .desktop_icons import LucideIcons
from .desktop_install import finish_install, start_installer
from .desktop_plan import PATIENT_FIELDS, apply_plan, job_scope, patient_name, read_plan, task_states, task_title, transfer_key
from .desktop_update import RELEASES, UpdateError, check_release, prepare_update
from .desktop_visits import CANCEL_URL, PETERSBURG, VISITS_URL, calendar_event, cancel_visit, fetch_visits, visit_scopes
from .desktop_worker import (
    ACTIVE_PHASES, CONTROL_LOCK, append_live_plan, job_phase, read_status, start_desktop_job, stop_job,
)
from .notify import Notifier
from .process_lock import process_lock
from .tls import build_bundle


JOB_ACTIVE = ACTIVE_PHASES | {'retry', 'checking_card'}
PHASES = {
    'idle': 'Поиск выключен', 'starting': 'Запускается', 'running': 'Поиск идёт',
    'checking_card': 'Проверяем медкарту', 'attachment_denied': 'Запись отклонена',
    'stopping': 'Останавливается', 'stopped': 'Поиск остановлен', 'completed': 'Приём подтверждён',
    'closed': 'Поиск завершён', 'retry': 'Повторим опрос', 'failed': 'Ошибка запуска',
    'interrupted': 'Поиск прервался', 'draft': 'Черновик',
}
MAX_PATIENT_AGE = 120
FIELD_LABELS = {'district': 'район', 'clinic': 'поликлинику', 'specialty': 'специальность'}


def parse_date(text):
    text = text.strip()
    if '.' in text:
        return datetime.strptime(text, '%d.%m.%Y').date()
    return date.fromisoformat(text)


def _birthdate_text(value):
    try:
        birthday = parse_date(value)
    except ValueError:
        return value
    return f'{birthday.day:02}.{birthday.month:02}.{birthday.year:04}'


def _person_key(profile):
    names = tuple((profile.get(key) or '').strip().casefold() for key in PATIENT_FIELDS[:-1])
    return (*names, _birthdate_text((profile.get('birthdate') or '').strip()))


class FormError(ValueError):
    def __init__(self, field, message):
        super().__init__(message)
        self.field = field


@dataclass
class SearchForm:
    mode: str
    patient: str
    start: str
    end: str
    period: str
    time_limited: bool = False
    time_from: str = ''
    time_to: str = ''
    choices: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)
    catalog_level: str = ''
    catalog_note: str = ''
    catalog_error: bool = False
    catalog_generation: int = 0
    source_generation: int = 0
    source_busy: bool = False
    source_note: str = ''
    card_checking: bool = False
    card_error: str = ''

    @classmethod
    def from_job(cls, job, default_patient):
        target = job.get('target', {})
        source = target.get('reschedule_from')
        mode = 'transfer' if source else 'book' if not target or target.get('auto_book') else ''
        start = parse_date(target.get('book_date_from') or date.today().isoformat())
        end = parse_date(target['book_date_to']) if target.get('book_date_to') else None
        period = 'day' if end and start == end else 'range' if end else 'from'
        time_from = target.get('book_time_from') or ''
        time_to = target.get('book_time_to') or ''
        form = cls(mode, job.get('patient', default_patient) or '', start.strftime('%d.%m.%Y'),
                   end.strftime('%d.%m.%Y') if end else '', period,
                   time_limited=bool(time_from or time_to), time_from=time_from, time_to=time_to)
        spec = source['specialty_id'] if source else next(iter(target.get('specialty_ids', [])), None)
        doctor = next(iter(target.get('doctor_ids', [])), '')
        for key, value, label in (
            ('district', target.get('district_id'), target.get('district_name')),
            ('clinic', target.get('lpu_id'), target.get('lpu_name')),
            ('specialty', spec, target.get('specialty_label')),
            ('doctor', doctor, target.get('doctor_label')),
        ):
            if value:
                form.choices[key] = {'id': value, 'name': label or str(value)}
        if 'clinic' in form.choices:
            form.choices['clinic']['phone'] = target.get('lpu_phone')
        form.choices.setdefault('doctor', ANY_DOCTOR)
        form.options['doctor'] = [ANY_DOCTOR]
        if source:
            option = {'id': source['appointment_id'], 'name': visit_label(source['visit_start']), 'source': source}
            form.options['source'] = [option]
            form.choices['source'] = option
        return form

    def clear_source(self):
        self.source_generation += 1
        self.choices.pop('source', None)
        self.options.pop('source', None)
        self.source_busy = False
        self.source_note = ''

    def time_bounds(self):
        if not self.time_limited:
            return None, None
        bounds = []
        for key in ('time_from', 'time_to'):
            value = getattr(self, key)
            try:
                bounds.append(parse_book_time('' if value == '__:__' else value))
            except ValueError as error:
                raise FormError(key, str(error)) from None
        start, end = bounds
        if not start and not end:
            raise FormError('time_from', 'Укажи время начала или окончания интервала.')
        if start and end and start > end:
            raise FormError('time_to', 'Время окончания должно быть не раньше начала.')
        return start, end

    def build(self, prior, patients):
        if not self.mode:
            raise FormError('mode', 'Выбери «Автозапись» или «Автоперенос».')
        for key, label in FIELD_LABELS.items():
            if key not in self.choices:
                raise FormError(key, f'Выбери {label} из списка.')
        try:
            start = parse_date(self.start)
            if start < date.today() and start.isoformat() != prior.get('book_date_from'):
                raise ValueError
        except ValueError:
            raise FormError('start', 'Введи сегодняшнюю или будущую дату.') from None
        end = start if self.period == 'day' else None
        if self.period == 'range':
            try:
                end = parse_date(self.end)
                if end < start:
                    raise ValueError
            except ValueError:
                raise FormError('end', 'Конечная дата должна быть не раньше начальной.') from None
        time_from, time_to = self.time_bounds()
        if self.patient not in patients:
            raise FormError('patient', 'Выбери пациента для записи или переноса.')
        district, clinic, spec = (self.choices[key] for key in FIELD_LABELS)
        doctor = self.choices.get('doctor', ANY_DOCTOR)
        target = {
            'district_id': district['id'], 'district_name': district['name'],
            'lpu_id': clinic['id'], 'lpu_name': clinic['name'], 'lpu_phone': clinic.get('phone'),
            'specialty_ids': [str(spec['id'])], 'specialty_label': spec['name'],
            'doctor_ids': [str(doctor['id'])] if doctor['id'] else [],
            'book_date_from': start.isoformat(), 'book_date_to': end.isoformat() if end else None,
            'book_time_from': time_from, 'book_time_to': time_to,
            'auto_book': True, 'notify_out_of_window': False,
        }
        if doctor['id']:
            target['doctor_label'] = doctor['name']
        if self.mode == 'transfer':
            source = self.choices.get('source', {}).get('source')
            if not source:
                raise FormError('source', 'Найди и выбери исходный приём.')
            if start <= datetime.fromisoformat(source['visit_start']).date():
                raise FormError('start', 'Выбери дату позже исходного приёма.')
            target['reschedule_from'] = source
            if prior.get('reschedule_from') == source:
                for key in ('stop_if_same_specialty', 'notify_out_of_window'):
                    if key in prior:
                        target[key] = prior[key]
        return {'target': target, 'patient': self.patient, 'consent': True}


class DesktopApp(QObject):
    changed = Signal()
    noticeChanged = Signal()
    updateChanged = Signal()
    focusRequested = Signal(str)
    confirmationRequested = Signal(str, str)
    visitCancellationRequested = Signal(str)

    def __init__(self, *, load_catalog=True):
        super().__init__()
        self.load_catalog = load_catalog
        self.legacy_profile = load_profile() or {}
        self.config = _read_json(CONFIG_PATH, default={})
        self.plan = read_plan(self.config, self.legacy_profile)
        self.runtime = read_status()
        self.states = task_states(load_state(), self.plan)
        self._visits = {}
        self._visits_failed = set()
        self._visits_unverified = set()
        self._visits_busy = ''
        self._pending_visit = None
        self.tasks = SimpleQueue()
        self.forms = {}
        self.drafts = []
        self.open_jobs = self._reopen_jobs()
        self.selected = next((key for key in self.plan['jobs'] if key in self.open_jobs), '')
        self.patient_id = next(iter(self.plan['patients']), '')
        self.patient_form = self._patient_values(self.patient_id)
        self.telegram_form = dict(self.config.get('telegram', {}))
        self.telegram_busy = False
        self.update_release = None
        self.update_gate = None
        self._update = {'version': __version__, 'available': '', 'state': 'idle', 'message': '',
                        'url': RELEASES + '/latest', 'hasRelease': False}
        self._notices = {}
        self.pending = None
        if not self.selected:
            self.newSearch()
        else:
            self.selectSearch(self.selected)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(550)

    @property
    def form(self):
        return self.forms[self.selected]

    def _phase(self, key):
        form = self.forms.get(key)
        if form and form.card_checking:
            return 'checking_card'
        if key in self.drafts:
            return 'draft'
        job = self.plan['jobs'].get(key)
        phase = job_phase(self.runtime, job, self.states.get(key, {}))
        if phase == 'attachment_denied' and form and (
            form.patient != job.get('patient')
            or str(form.choices.get('clinic', {}).get('id')) != str(job['target']['lpu_id'])
        ):
            return 'stopped'
        return phase

    def _reopen_jobs(self):
        reopened = set()
        for key in self.plan['jobs']:
            transfers = self.states.get(key, {}).get('reschedules', {})
            if self._phase(key) in ('starting', 'running', 'retry', 'attachment_denied') or any(
                transfer.get('status') == 'submitted' for transfer in transfers.values()
            ):
                reopened.add(key)
        return reopened

    @Property('QVariantMap', notify=changed)
    def view(self):
        patients = self.plan['patients']
        options = [{'id': key, 'name': patient_name(profile)} for key, profile in patients.items()]
        form = self.form
        phase = self._phase(self.selected)
        stamp = self.states.get(self.selected, {}).get('last_scan')
        saved = [key for key in self.plan['jobs'] if key in self.open_jobs]
        tabs = []
        for key in (*saved, *self.drafts):
            job = self.plan['jobs'].get(key)
            title = task_title(job, patients) if job else 'Новый поиск'
            tabs.append({'id': key, 'name': title or 'Поиск', 'phase': self._phase(key)})
        return {
            'selected': self.selected, 'tabs': tabs, 'phase': phase, 'status': PHASES[phase],
            'lastPoll': stamp[11:16] if stamp else '',
            'cardError': form.card_error,
            'editable': phase not in JOB_ACTIVE, 'mode': form.mode, 'patient': form.patient,
            'patientLabel': patient_name(patients.get(form.patient, {})), 'patients': options,
            'start': form.start, 'end': form.end, 'period': form.period,
            'timeLimited': form.time_limited, 'timeFrom': form.time_from, 'timeTo': form.time_to,
            'choices': {key: (value.get('label') or value['name']) for key, value in form.choices.items()},
            'options': form.options, 'catalogNote': form.catalog_note, 'catalogError': form.catalog_error,
            'sourceBusy': form.source_busy, 'sourceNote': form.source_note,
            'patientId': self.patient_id, 'patientForm': {key: self.patient_form.get(key, '') for key in PATIENT_FIELDS},
            'patientEditable': self.runtime['phase'] not in ACTIVE_PHASES or
            (self._can_add_patient() and not self.patient_id),
            'canAddPatient': self.runtime['phase'] not in ACTIVE_PHASES or self._can_add_patient(),
            'telegram': {
                'enabled': bool(self.telegram_form.get('enabled')),
                'bot_token': self.telegram_form.get('bot_token') or '',
                'chat_id': str(self.telegram_form.get('chat_id') or ''),
                'busy': self.telegram_busy,
            },
        }

    @Property('QVariantMap', notify=noticeChanged)
    def notices(self):
        return self._notices

    def _notice(self, page, text='', *, error=False, field=''):
        notice = {'text': text, 'error': error, 'field': field}
        if notice != self._notices.get(page):
            self._notices = {**self._notices, page: notice}
            self.noticeChanged.emit()
        if field:
            self.focusRequested.emit(field)

    def _apply_config(self, config):
        previous = self.plan['patients']
        self.config = config
        self.plan = read_plan(config, self.legacy_profile)
        self.drafts = [key for key in self.drafts if key not in self.plan['jobs']]
        for scope in list(self._visits):
            key = scope[0]
            if _person_key(previous.get(key, {})) != _person_key(self.plan['patients'].get(key, {})):
                del self._visits[scope]
                self._visits_failed.discard(scope)

    @Slot()
    def _refresh(self):
        while not self.tasks.empty():
            callback, result, error = self.tasks.get_nowait()
            callback(result, error)
        config = _read_json(CONFIG_PATH, default={})
        status = read_status()
        states = task_states(load_state(), read_plan(config, self.legacy_profile))
        if (config, status, states) == (self.config, self.runtime, self.states):
            return
        was_active = self._phase(self.selected) in JOB_ACTIVE
        if config != self.config:
            self._apply_config(config)
        self.runtime, self.states = status, states
        self.open_jobs.update(self._reopen_jobs())
        self.changed.emit()
        if was_active and self._phase(self.selected) not in JOB_ACTIVE and self.load_catalog:
            self._ensure_catalog()

    def _background(self, operation, callback):
        def run():
            try:
                result = operation()
            except (GorzdravError, OSError, TypeError, ValueError, KeyError, RuntimeError) as error:
                self.tasks.put((callback, None, error))
            else:
                self.tasks.put((callback, result, None))
        threading.Thread(target=run, daemon=True).start()

    def _client(self):
        return GorzdravClient(verify=build_bundle(), timeout=15,
                              direct_interface=self.config.get('http', {}).get('direct_interface'))

    def _upcoming_visits(self, scopes):
        now = datetime.now(PETERSBURG)
        visits = {visit.key: visit for scope, rows in self._visits.items() if scope in scopes
                  for visit in rows if visit.start > now}
        return sorted(visits.values(), key=lambda item: (item.start, item.patient, item.clinic))

    @Property('QVariantMap', notify=changed)
    def visits(self):
        scopes = visit_scopes(self.plan)
        return {
            'available': bool(scopes), 'busy': self._visits_busy,
            'rows': [{
                'id': visit.key, 'date': visit.start.strftime('%d.%m.%Y'), 'time': visit.start.strftime('%H:%M'),
                'title': visit.title, 'doctor': visit.doctor if visit.doctor != visit.title else '',
                'patient': visit.patient, 'clinic': visit.clinic, 'place': visit.place,
                'portalCancel': visit.referral or visit.scope in self._visits_failed or
                visit.key in self._visits_unverified,
            } for visit in self._upcoming_visits(scopes)],
        }

    @Slot()
    def refreshVisits(self):
        scopes = visit_scopes(self.plan)
        if not scopes or self._visits_busy or self._pending_visit is not None:
            return
        self._visits_busy = 'refresh'
        self._notice('visits')
        self.changed.emit()

        def done(results, error):
            self._visits_busy = ''
            current = visit_scopes(self.plan)
            for scope, (profile, _) in scopes.items():
                if scope not in current or _person_key(profile) != _person_key(current[scope][0]):
                    continue
                rows = None if error else results[scope]
                if rows is None:
                    self._visits_failed.add(scope)
                else:
                    self._visits_unverified.difference_update(visit.key for visit in self._visits.get(scope, []))
                    self._visits[scope] = rows
                    self._visits_failed.discard(scope)
            failed = self._visits_failed.intersection(current)
            if failed:
                message = ('Не удалось обновить часть талонов. Попробуй ещё раз или проверь на Горздраве.'
                           if failed != current.keys() else
                           'Не удалось загрузить талоны. Попробуй ещё раз или проверь на Горздраве.')
                self._notice('visits', message, error=True)
            self.changed.emit()

        self._background(lambda: fetch_visits(self._client(), scopes), done)

    @Slot()
    def openVisitsPortal(self):
        self._open_visits_url(VISITS_URL)

    @Slot()
    def openCancellationPortal(self):
        self._open_visits_url(CANCEL_URL)

    def _open_visits_url(self, url):
        if not QDesktopServices.openUrl(QUrl(url)):
            self._notice('visits', f'Не удалось открыть браузер. Адрес страницы — {url}', error=True)

    def _visit(self, key):
        return next((visit for visit in self._upcoming_visits(visit_scopes(self.plan)) if visit.key == key), None)

    @Slot(str)
    def addVisitToCalendar(self, key):
        visit = self._visit(key)
        if visit is None:
            return
        folder = ROOT / '.cache' / 'calendar'
        path = folder / f'Приём-{key[:16]}.ics'
        try:
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_bytes(calendar_event(visit))
        except OSError:
            self._notice('visits', 'Не удалось сохранить событие календаря.', error=True)
            return
        if QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            self._notice('visits', 'Подтверди добавление события в календаре.')
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
            self._notice('visits', f'Открой или импортируй файл «{path.name}» в календаре.', error=True)

    @Slot(str)
    def requestVisitCancellation(self, key):
        if self._visits_busy or self._pending_visit is not None:
            return
        visit = self._visit(key)
        if visit is None:
            return
        if visit.referral or visit.scope in self._visits_failed or key in self._visits_unverified:
            self.openCancellationPortal()
            return
        self._pending_visit = visit
        when = visit.start.strftime('%d.%m.%Y в %H:%M')
        self.visitCancellationRequested.emit('\n'.join(filter(None, (
            f'{when} · {visit.title}', visit.doctor if visit.doctor != visit.title else '',
            visit.clinic, visit.place, visit.patient,
        ))))

    @Slot(bool)
    def confirmVisitCancellation(self, accepted):
        visit, self._pending_visit = self._pending_visit, None
        if not accepted or visit is None or self._visits_busy or self._visit(visit.key) != visit:
            return
        profile = self.plan['patients'][visit.scope[0]]
        self._visits_busy = visit.key
        self._notice('visits', 'Проверяем талон и отправляем отмену…')
        self.changed.emit()

        def operation():
            with process_lock(ROOT / '.cache' / 'visit-cancel.lock'):
                plan = read_plan(_read_json(CONFIG_PATH, default={}), load_profile())
                current_profile = plan['patients'].get(visit.scope[0], {})
                if _person_key(profile) != _person_key(current_profile):
                    return 'profile_changed'
                status = read_status()
                states = task_states(load_state(), plan)
                for key, job in plan['jobs'].items():
                    if transfer_key(job) != (visit.scope[1], visit.scope[0], visit.appointment_id):
                        continue
                    state = states[key]
                    if (job_phase(status, job, state) in JOB_ACTIVE or
                            state['reschedules'].get(visit.appointment_id, {}).get('status') == 'submitted'):
                        return 'transfer'
                return cancel_visit(self._client(), visit, current_profile)

        def done(result, error):
            self._visits_busy = ''
            if error:
                message = ('Отмена уже проверяется в другом окне.' if isinstance(error, BlockingIOError) else
                           'Не удалось проверить талон. Попробуй ещё раз или отмени на Горздраве.')
            elif result in ('cancelled', 'absent'):
                self._visits[visit.scope] = [row for row in self._visits.get(visit.scope, []) if row.key != visit.key]
                message = ('Горздрав подтвердил отмену приёма.' if result == 'cancelled' else
                           'Этот талон больше не отображается на Горздраве.')
            elif result == 'transfer':
                message = 'Для этого талона идёт автоперенос. Проверь его состояние в поиске.'
            elif result == 'profile_changed':
                message = 'Данные пациента изменились. Обнови список записей.'
            else:
                self._visits_unverified.add(visit.key)
                message = ('Этот талон можно отменить на сайте Горздрава.' if result == 'referral' else
                           'Горздрав пока не подтвердил отмену. Проверь талон на сайте.')
            self._notice('visits', message, error=result not in ('cancelled', 'absent'))
            self.changed.emit()

        self._background(operation, done)

    @Slot()
    def newSearch(self):
        key = uuid4().hex
        self.drafts.append(key)
        self.selectSearch(key)

    @Slot(str)
    def selectSearch(self, key):
        if key not in self.open_jobs and key not in self.drafts:
            return
        self.selected = key
        if key not in self.forms:
            self.forms[key] = SearchForm.from_job(self.plan['jobs'].get(key, {}), self.patient_id)
        self._notice('search')
        self.changed.emit()
        if self.load_catalog:
            self._ensure_catalog()

    def _ensure_catalog(self):
        if self._phase(self.selected) not in JOB_ACTIVE and 'district' not in self.form.options:
            if not self.form.catalog_level:
                self._load_catalog(self.form, 'district')

    @Slot(str, str)
    def setSearch(self, name, value):
        if self._phase(self.selected) in JOB_ACTIVE:
            return
        if name in ('start', 'end', 'time_from', 'time_to'):
            setattr(self.form, name, value)
        elif name == 'mode' and value in ('book', 'transfer'):
            self.form.mode = value
        elif name == 'period' and value in ('from', 'range', 'day'):
            self.form.period = value
        else:
            return
        self._notice('search')
        self.changed.emit()

    @Slot(bool)
    def limitSearchTime(self, enabled):
        if self._phase(self.selected) in JOB_ACTIVE:
            return
        self.form.time_limited = enabled
        self._notice('search')
        self.changed.emit()

    @Slot('QVariantList', str, result='QVariantList')
    def filterChoices(self, choices, query):
        return match_choices(choices, query)

    @Slot(str, str)
    def choose(self, name, key):
        if self._phase(self.selected) in JOB_ACTIVE:
            return
        form = self.form
        if name == 'patient':
            if key in self.plan['patients'] and key != form.patient:
                form.patient = key
                form.card_error = ''
                form.clear_source()
        else:
            option = next((row for row in form.options.get(name, []) if str(row['id']) == key), None)
            if option is None:
                return
            previous = form.choices.get(name, {})
            if str(previous.get('id')) == key:
                return
            form.choices[name] = option
            if name in ('district', 'clinic'):
                form.card_error = ''
            if name in FIELD_LABELS:
                form.clear_source()
                next_level = CATALOG.index(name) + 1
                for child in CATALOG[next_level:]:
                    form.choices.pop(child, None)
                    form.options.pop(child, None)
                self._load_catalog(form, CATALOG[next_level])
        self._notice('search')
        self.changed.emit()

    def _load_catalog(self, form, level):
        form.catalog_generation += 1
        generation = form.catalog_generation
        form.catalog_level = level
        form.catalog_note = 'Загружаем список…'
        form.catalog_error = False
        choices = form.choices.copy()
        self.changed.emit()

        def done(records, error):
            if generation != form.catalog_generation:
                return
            if error:
                form.catalog_note = 'Список не загрузился.'
                form.catalog_error = True
            else:
                form.options[level] = records
                form.catalog_level = ''
                form.catalog_note = '' if records else 'Горздрав вернул пустой список.'
                selected = form.choices.get(level)
                match = next((row for row in records
                              if selected and str(row['id']) == str(selected['id'])), None)
                if match:
                    form.choices[level] = match
                    if level != 'doctor':
                        self._load_catalog(form, CATALOG[CATALOG.index(level) + 1])
                        return
                if level == 'doctor' and not selected:
                    form.choices['doctor'] = ANY_DOCTOR
            self.changed.emit()

        self._background(lambda: fetch_catalog(self._client(), level, choices), done)

    @Slot()
    def retryCatalog(self):
        if self.form.catalog_error and self._phase(self.selected) not in JOB_ACTIVE:
            self._load_catalog(self.form, self.form.catalog_level)

    @Slot()
    def findTransfer(self):
        if self._phase(self.selected) in JOB_ACTIVE:
            return
        form = self.form
        for key, label in FIELD_LABELS.items():
            if key not in form.choices:
                self._notice('search', f'Выбери {label} из списка.', error=True, field=key)
                return
        profile = self.plan['patients'].get(form.patient)
        if not profile:
            self._notice('search', 'Выбери пациента для переноса.', error=True, field='patient')
            return
        clinic, specialty = (str(form.choices[key]['id']) for key in ('clinic', 'specialty'))
        form.source_generation += 1
        generation = form.source_generation
        form.source_busy = True
        form.source_note = 'Проверяем записи на Горздраве…'
        self._notice('search')
        self.changed.emit()

        def done(records, error):
            if form.source_generation != generation:
                return
            form.source_busy = False
            if error:
                form.source_note = 'Записи не загрузились. Попробуй ещё раз.'
            else:
                form.options['source'] = records
                form.choices.pop('source', None)
                if len(records) == 1:
                    form.choices['source'] = records[0]
                form.source_note = ('Подходящих записей нет.' if not records else
                                    'Выбери исходный приём.' if len(records) > 1 else '')
            self.changed.emit()

        self._background(lambda: fetch_appointments(self._client(), clinic, specialty, profile), done)

    @Slot()
    def searchAction(self):
        phase = self._phase(self.selected)
        if phase in ('stopping', 'checking_card'):
            return
        if phase in JOB_ACTIVE:
            try:
                job = self.plan['jobs'][self.selected]
                stop_job(self.selected, self.runtime['id'], token=job.get('run', {}).get('token'))
            except (OSError, ValueError) as error:
                self._notice('search', str(error), error=True)
            else:
                self._notice('search')
                self._refresh()
            return
        tab = self.selected
        key = tab
        prior = self.plan['jobs'].get(tab, {})
        try:
            job = self.form.build(prior.get('target', {}), self.plan['patients'])
            scope = job_scope(job)
            transfer = transfer_key(job)
            for other, existing in self.plan['jobs'].items():
                if other == tab:
                    continue
                same_transfer = transfer and transfer == transfer_key(existing)
                if not same_transfer and job_scope(existing) != scope:
                    continue
                if other in self.open_jobs or tab not in self.drafts or key != tab:
                    raise ValueError('Для этого приёма уже есть автоперенос.' if same_transfer
                                     else 'Такой поиск уже сохранён.')
                key, prior = other, existing
        except (ValueError, KeyError) as error:
            self._notice('search', str(error), error=True, field=getattr(error, 'field', ''))
            return
        if prior and job_scope(prior) == scope:
            job = {**prior, **job, 'target': {**prior['target'], **job['target']}}
        else:
            job['revision'] = uuid4().hex
        self.pending = (tab, key, job)
        target = job['target']
        source = target.get('reschedule_from')
        title = 'Подтвердить автоперенос' if source else 'Подтвердить автозапись'
        patient = patient_name(self.plan['patients'][job['patient']])
        window = target['book_date_from']
        if target['book_date_to']:
            window += ' — ' + target['book_date_to'] if target['book_date_to'] != window else ''
        else:
            window = 'с ' + window
        summary = f'{patient}\n{target["specialty_label"]} · {target["lpu_name"]}\nДаты поиска: {window}'
        if hours := format_time_window(target):
            summary += f'\nВремя приёма {hours}'
        if source:
            summary = f'Текущий приём: {visit_label(source["visit_start"])}\n' + summary
        self.confirmationRequested.emit(title, summary)

    @Slot(bool)
    def confirmSearch(self, accepted):
        pending, self.pending = self.pending, None
        if not accepted or not pending:
            return
        tab, key, job = pending
        form = self.forms[tab]
        profile = self.plan['patients'].get(job['patient'])
        self._notice('search')
        if not profile_is_ready(profile):
            form.card_error = 'Заполни ФИО и дату рождения в разделе «Пациенты».'
            self.changed.emit()
            return
        form.card_error = ''
        form.card_checking = True
        self.changed.emit()

        def check():
            client = self._client()
            with client.s:
                return clinic_patient_id(client, str(job['target']['lpu_id']), profile, fresh=True)

        def done(card_id, error):
            form.card_checking = False
            if error:
                form.card_error = 'Не удалось проверить медкарту на Горздраве. Запусти поиск ещё раз.'
            elif not card_id:
                form.card_error = ('Поликлиника не нашла медкарту. Проверь ФИО и дату рождения. '
                                   'Если всё верно, уточни данные в регистратуре.')
            else:
                self._start_checked_search(tab, key, job, (profile, card_id))
            self.changed.emit()

        self._background(check, done)

    def _start_checked_search(self, tab, key, job, checked_card):
        try:
            start_desktop_job(key, job, self.legacy_profile, checked_card=checked_card)
        except (OSError, ValueError) as error:
            self.forms[tab].card_error = str(error)
        else:
            if tab != key:
                self.forms[key] = self.forms.pop(tab)
                self.drafts.remove(tab)
                if self.selected == tab:
                    self.selected = key
            self.open_jobs.add(key)
            self._refresh()

    @Slot()
    def openAttachmentCheck(self):
        if not QDesktopServices.openUrl(QUrl(ATTACHMENT_CHECK_URL)):
            self._notice('search', 'Не удалось открыть страницу проверки прикрепления.', error=True)

    def _can_add_patient(self):
        return self.runtime['phase'] == 'running' and self.runtime.get('live_plan', False)

    @Slot(str)
    def selectPatient(self, key):
        self.patient_id = key if key in self.plan['patients'] else ''
        self.patient_form = self._patient_values(self.patient_id)
        self._notice('patient')
        self.changed.emit()

    def _patient_values(self, key):
        values = dict(self.plan['patients'].get(key, {}))
        values['birthdate'] = _birthdate_text(values.get('birthdate') or '')
        return values

    @Slot(str, str)
    def setPatient(self, key, value):
        if key in PATIENT_FIELDS:
            self.patient_form[key] = value
            self._notice('patient')

    @Slot()
    def savePatient(self):
        values = {key: self.patient_form.get(key, '').strip() for key in PATIENT_FIELDS}
        try:
            for key, label in (('first_name', 'имя'), ('last_name', 'фамилию')):
                if not values[key]:
                    raise FormError(key, f'Введи {label} пациента.')
            try:
                if not re.fullmatch(r'[0-9]{2}\.[0-9]{2}\.[0-9]{4}', values['birthdate']):
                    raise ValueError
                birthday = parse_date(values['birthdate'])
            except ValueError:
                raise FormError('birthdate', 'Введи существующую дату в формате ДД.ММ.ГГГГ.') from None
            today = date.today()
            earliest_year = today.year - MAX_PATIENT_AGE
            earliest_day = min(today.day, monthrange(earliest_year, today.month)[1])
            earliest = date(earliest_year, today.month, earliest_day)
            if not earliest <= birthday <= today:
                raise FormError('birthdate', f'Дата рождения должна быть с {earliest:%d.%m.%Y} по {today:%d.%m.%Y}.')
            key = self.patient_id or uuid4().hex
            live = self.runtime['phase'] in ACTIVE_PHASES
            if live:
                if not self._can_add_patient() or self.patient_id:
                    raise ValueError('Во время опроса можно добавить новый профиль.')
                self._validate_patient(self.plan, key, values)
                config = append_live_plan(self.runtime['id'], patient=(key, values))
            else:
                with process_lock(CONTROL_LOCK):
                    if read_status()['phase'] in ACTIVE_PHASES:
                        raise ValueError('Останови поиск перед изменением профиля.')
                    raw = _read_json(CONFIG_PATH, default={})
                    plan = read_plan(raw, self.legacy_profile)
                    self._validate_patient(plan, key, values)
                    prior = plan['patients'].get(key, {})
                    plan['patients'][key] = {**prior, **values} if _person_key(prior) == _person_key(values) else values
                    config = apply_plan(raw, plan)
                    _write_json(CONFIG_PATH, config)
        except (OSError, ValueError) as error:
            self._notice('patient', str(error), error=True, field=getattr(error, 'field', ''))
        else:
            self._apply_config(config)
            self.selectPatient(key)
            self._notice('patient', 'Пациент сохранён.')

    @staticmethod
    def _validate_patient(plan, key, values):
        patients = plan['patients']
        if any(other != key and _person_key(profile) == _person_key(values)
               for other, profile in patients.items()):
            raise FormError('last_name', 'Такой профиль уже сохранён.')
        old = patients.get(key)
        if old and _person_key(old) != _person_key(values):
            if any(job.get('patient') == key for job in plan['jobs'].values()):
                raise ValueError('Этот профиль связан с поиском. Для другого человека добавь профиль.')

    @Slot(str, str)
    def setTelegram(self, key, value):
        if key in ('bot_token', 'chat_id'):
            self.telegram_form[key] = value
            self._notice('telegram')

    @Slot(bool)
    def toggleTelegram(self, enabled):
        self.telegram_form['enabled'] = enabled
        self._notice('telegram')
        self.changed.emit()

    def _telegram_settings(self):
        token = (self.telegram_form.get('bot_token') or '').strip()
        chat = str(self.telegram_form.get('chat_id') or '').strip()
        enabled = bool(self.telegram_form.get('enabled'))
        if enabled:
            if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', token):
                raise FormError('bot_token', 'Проверь токен бота.')
            if not re.fullmatch(r'-?[1-9]\d*', chat):
                raise FormError('chat_id', 'ID чата должен быть числом.')
        return {**self.config.get('telegram', {}), 'enabled': enabled, 'bot_token': token, 'chat_id': chat}

    @Slot()
    def saveTelegram(self):
        try:
            telegram = self._telegram_settings()
            with process_lock(CONTROL_LOCK):
                raw = _read_json(CONFIG_PATH, default={})
                config = {**raw, 'telegram': telegram}
                _write_json(CONFIG_PATH, config)
        except (OSError, ValueError) as error:
            self._notice('telegram', str(error), error=True, field=getattr(error, 'field', ''))
        else:
            self._apply_config(config)
            self._notice('telegram', 'Настройки вступят в силу при следующем запуске фонового поиска.'
                         if self.runtime['phase'] in ACTIVE_PHASES else 'Настройки сохранены.')
            self.changed.emit()

    @Slot()
    def testTelegram(self):
        try:
            telegram = self._telegram_settings()
        except FormError as error:
            self._notice('telegram', str(error), error=True, field=error.field)
            return
        if self.telegram_busy or not telegram['enabled']:
            return
        self.telegram_busy = True
        self._notice('telegram', 'Отправляем тестовое сообщение…')
        self.changed.emit()

        def done(result, error):
            self.telegram_busy = False
            self._notice('telegram', 'Сообщение отправлено.' if result else
                         'Отправка не удалась. Проверь токен, ID чата и соединение.', error=not result)
            self.changed.emit()

        self._background(lambda: Notifier({'telegram': telegram}).telegram(
            'Талоны Горздрава. Проверка уведомлений.'), done)

    @Property('QVariantMap', notify=updateChanged)
    def update(self):
        return self._update

    def _set_update(self, state, message=''):
        release = self.update_release
        self._update = {'version': __version__, 'available': release.version if release else '',
                        'state': state, 'message': message, 'hasRelease': release is not None,
                        'url': release.url if release else RELEASES + '/latest'}
        self.updateChanged.emit()

    @Slot()
    def checkForUpdate(self):
        if self._update['state'] in ('checking', 'installing'):
            return
        self._set_update('checking', 'Проверяем обновления…')

        def done(release, error):
            self.update_release = release
            if error:
                self._set_update('error', str(error) if isinstance(error, UpdateError) else
                                 'Не удалось проверить обновления.')
            else:
                self._set_update('available' if release else 'current',
                                 'После установки приложение перезапустится.' if release else
                                 'Установлена последняя версия.')

        self._background(check_release, done)

    @Slot()
    def installUpdate(self):
        release = self.update_release
        if release is None or self._update['state'] in ('checking', 'installing'):
            return
        self._set_update('installing', 'Скачиваем и проверяем обновление…')

        def prepare():
            work, bundle = prepare_update(release)
            return start_installer(work, bundle)

        def done(gate, error):
            if error:
                self._set_update('error', str(error) if isinstance(error, UpdateError) else
                                 'Не удалось подготовить установку. Можно скачать архив с GitHub.')
                return
            self.update_gate = gate
            QGuiApplication.instance().quit()

        self._background(prepare, done)

    def updateFinished(self, work):
        self._set_update('installing', 'Завершаем обновление…')

        def done(message, error):
            if message or error:
                self._set_update('error', message or 'Не удалось подтвердить результат обновления.')
            else:
                self._set_update('current', 'Обновление установлено.')

        self._background(lambda: finish_install(work), done)

    def releaseUpdateLock(self):
        if self.update_gate is not None:
            self.update_gate.__exit__(None, None, None)


def create_window(*, load_catalog=True, check_updates=False):
    QQuickStyle.setStyle('Basic')
    application = QGuiApplication([])
    application.setApplicationName('Талоны Горздрава')
    application.setApplicationVersion(__version__)
    controller = DesktopApp(load_catalog=load_catalog)
    engine = QQmlApplicationEngine()
    engine.addImageProvider('lucide', LucideIcons())
    engine.setInitialProperties({'bridge': controller})
    engine.load(QUrl.fromLocalFile(str(Path(__file__).with_name('qml') / 'Main.qml')))
    if not engine.rootObjects() or not isinstance(engine.rootObjects()[0], QQuickWindow):
        raise RuntimeError('Не удалось открыть окно Qt Quick.')
    if check_updates and getattr(sys, 'frozen', False):
        QTimer.singleShot(600, controller.checkForUpdate)
    return application, engine, controller
