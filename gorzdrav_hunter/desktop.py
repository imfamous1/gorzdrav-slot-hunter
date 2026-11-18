"""Контроллер настольного интерфейса Qt Quick."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from queue import SimpleQueue
import re
import threading
from uuid import uuid4

from PySide6.QtCore import QObject, Property, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtQuickControls2 import QQuickStyle

from .client import GorzdravClient, GorzdravError
from .config import CONFIG_PATH, _read_json, _write_json, load_profile, load_state
from .desktop_catalog import ANY_DOCTOR, CATALOG, fetch_appointments, fetch_catalog, match_choices, visit_label
from .desktop_plan import apply_plan, patient_name, read_plan, task_states, task_title, transfer_key
from .desktop_worker import (
    ACTIVE_PHASES, CONTROL_LOCK, EVENTS, append_live_plan, job_phase, read_status, start_desktop_job, stop_job,
)
from .notify import Notifier
from .process_lock import process_lock
from .tls import build_bundle


JOB_ACTIVE = ACTIVE_PHASES | {'retry'}
PHASES = {
    'idle': 'Поиск выключен', 'starting': 'Запускается', 'running': 'Поиск идёт',
    'stopping': 'Останавливается', 'stopped': 'Поиск остановлен', 'completed': 'Приём подтверждён',
    'closed': 'Поиск завершён', 'retry': 'Повторим опрос', 'failed': 'Ошибка запуска',
    'interrupted': 'Поиск прервался', 'draft': 'Черновик',
}
EVENT_LABELS = {'OK': 'Подтверждение', 'WARN': 'Предупреждение', 'ERR': 'Ошибка', 'SLOT': 'Свободный талон'}
PATIENT_FIELDS = ('last_name', 'first_name', 'middle_name', 'birthdate')
FIELD_LABELS = {'district': 'район', 'clinic': 'поликлинику', 'specialty': 'специальность'}


def parse_date(text):
    text = text.strip()
    if '.' in text:
        return datetime.strptime(text, '%d.%m.%Y').date()
    return date.fromisoformat(text)


def _person_key(profile):
    return tuple((profile.get(key) or '').strip().casefold() for key in PATIENT_FIELDS)


def _job_scope(job):
    target = job['target']
    return (
        job.get('patient'), str(target['lpu_id']), target.get('reschedule_from'),
        tuple(target.get('specialty_ids', [])), tuple(target.get('doctor_ids', [])),
        target['book_date_from'], target.get('book_date_to'), target.get('auto_book'),
    )


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
    choices: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)
    catalog_level: str = ''
    catalog_note: str = ''
    catalog_error: bool = False
    catalog_generation: int = 0
    source_generation: int = 0
    source_busy: bool = False
    source_note: str = ''

    @classmethod
    def from_job(cls, job, default_patient):
        target = job.get('target', {})
        source = target.get('reschedule_from')
        mode = 'transfer' if source else 'book' if not target or target.get('auto_book') else ''
        start = parse_date(target.get('book_date_from') or date.today().isoformat())
        end = parse_date(target['book_date_to']) if target.get('book_date_to') else None
        period = 'day' if end and start == end else 'range' if end else 'from'
        form = cls(mode, job.get('patient', default_patient) or '', start.strftime('%d.%m.%Y'),
                   end.strftime('%d.%m.%Y') if end else '', period)
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
    focusRequested = Signal(str)
    confirmationRequested = Signal(str, str)

    def __init__(self, *, load_catalog=True):
        super().__init__()
        self.load_catalog = load_catalog
        self.legacy_profile = load_profile() or {}
        self.config = _read_json(CONFIG_PATH, default={})
        self.plan = read_plan(self.config, self.legacy_profile)
        self.runtime = read_status()
        self.states = task_states(load_state(), self.plan)
        self.events = _read_json(EVENTS, default=[])
        self.tasks = SimpleQueue()
        self.forms = {}
        self.drafts = []
        self.open_jobs = self._reopen_jobs()
        self.selected = next((key for key in self.plan['jobs'] if key in self.open_jobs), '')
        self.patient_id = next(iter(self.plan['patients']), '')
        self.patient_form = dict(self.plan['patients'].get(self.patient_id, {}))
        self.telegram_form = dict(self.config.get('telegram', {}))
        self.telegram_busy = False
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
        return ('draft' if key in self.drafts else
                job_phase(self.runtime, self.plan['jobs'].get(key), self.states.get(key, {})))

    def _reopen_jobs(self):
        reopened = set()
        for key in self.plan['jobs']:
            transfers = self.states.get(key, {}).get('reschedules', {})
            if self._phase(key) in ('starting', 'running', 'retry') or any(
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
            'editable': phase not in JOB_ACTIVE, 'mode': form.mode, 'patient': form.patient,
            'patientLabel': patient_name(patients.get(form.patient, {})), 'patients': options,
            'start': form.start, 'end': form.end, 'period': form.period,
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
            'events': [{'time': event['time'].replace('T', ' ')[:16], 'kind': event['kind'],
                        'label': EVENT_LABELS.get(event['kind'], 'Событие'), 'text': event['text']}
                       for event in reversed(self.events)],
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
        self.config = config
        self.plan = read_plan(config, self.legacy_profile)
        self.drafts = [key for key in self.drafts if key not in self.plan['jobs']]

    @Slot()
    def _refresh(self):
        while not self.tasks.empty():
            callback, result, error = self.tasks.get_nowait()
            callback(result, error)
        config = _read_json(CONFIG_PATH, default={})
        status = read_status()
        states = task_states(load_state(), read_plan(config, self.legacy_profile))
        events = _read_json(EVENTS, default=[])
        if (config, status, states, events) == (self.config, self.runtime, self.states, self.events):
            return
        was_active = self._phase(self.selected) in JOB_ACTIVE
        if config != self.config:
            self._apply_config(config)
        self.runtime, self.states, self.events = status, states, events
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
        if name in ('start', 'end'):
            setattr(self.form, name, value)
        elif name == 'mode' and value in ('book', 'transfer'):
            self.form.mode = value
        elif name == 'period' and value in ('from', 'range', 'day'):
            self.form.period = value
        else:
            return
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
                form.clear_source()
        else:
            option = next((row for row in form.options.get(name, []) if str(row['id']) == key), None)
            if option is None:
                return
            previous = form.choices.get(name, {})
            if str(previous.get('id')) == key:
                return
            form.choices[name] = option
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
        if phase == 'stopping':
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
            scope = _job_scope(job)
            transfer = transfer_key(job)
            for other, existing in self.plan['jobs'].items():
                if other == tab:
                    continue
                same_transfer = transfer and transfer == transfer_key(existing)
                if not same_transfer and _job_scope(existing) != scope:
                    continue
                if other in self.open_jobs or tab not in self.drafts or key != tab:
                    raise ValueError('Для этого приёма уже есть автоперенос.' if same_transfer
                                     else 'Такой поиск уже сохранён.')
                key, prior = other, existing
        except (ValueError, KeyError) as error:
            self._notice('search', str(error), error=True, field=getattr(error, 'field', ''))
            return
        if prior and _job_scope(prior) == scope:
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
        if source:
            summary = f'Текущий приём: {visit_label(source["visit_start"])}\n' + summary
        self.confirmationRequested.emit(title, summary)

    @Slot(bool)
    def confirmSearch(self, accepted):
        pending, self.pending = self.pending, None
        if not accepted or not pending:
            return
        tab, key, job = pending
        try:
            start_desktop_job(key, job, self.legacy_profile)
        except (OSError, ValueError) as error:
            self._notice('search', str(error), error=True)
        else:
            if tab != key:
                self.forms[key] = self.forms.pop(tab)
                self.drafts.remove(tab)
                if self.selected == tab:
                    self.selected = key
            self.open_jobs.add(key)
            self._notice('search')
            self._refresh()

    def _can_add_patient(self):
        return self.runtime['phase'] == 'running' and self.runtime.get('live_plan', False)

    @Slot(str)
    def selectPatient(self, key):
        self.patient_id = key if key in self.plan['patients'] else ''
        self.patient_form = dict(self.plan['patients'].get(self.patient_id, {}))
        self._notice('patient')
        self.changed.emit()

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
                birthday = parse_date(values['birthdate'])
                if birthday > date.today():
                    raise ValueError
            except ValueError:
                raise FormError('birthdate', 'Проверь дату рождения.') from None
            values['birthdate'] = birthday.strftime('%d.%m.%Y')
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


def create_window(*, load_catalog=True):
    QQuickStyle.setStyle('Basic')
    application = QGuiApplication([])
    application.setApplicationName('Талоны Горздрава')
    controller = DesktopApp(load_catalog=load_catalog)
    engine = QQmlApplicationEngine()
    engine.setInitialProperties({'bridge': controller})
    engine.load(QUrl.fromLocalFile(str(Path(__file__).with_name('qml') / 'Main.qml')))
    if not engine.rootObjects() or not isinstance(engine.rootObjects()[0], QQuickWindow):
        raise RuntimeError('Не удалось открыть окно Qt Quick.')
    return application, engine, controller
