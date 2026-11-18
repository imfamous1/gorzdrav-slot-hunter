"""Настольный интерфейс для настройки и наблюдения за фоновым поиском."""
from __future__ import annotations

from datetime import date, datetime
import queue
import re
import threading
import tkinter as tk
from tkinter import font, messagebox, ttk

from .client import GorzdravClient, GorzdravError
from .config import CONFIG_PATH, _read_json, load_profile, load_state
from .desktop_worker import EVENTS, read_status, request_stop, start_search
from .notify import Notifier
from .tls import build_bundle

BG = '#F2F5F4'
SURFACE = '#FFFFFF'
INK = '#182F29'
MUTED = '#48605A'
ACCENT = '#12634E'
BORDER = '#B5C5BF'
ERROR = '#9F3126'
ACTIVE = {'starting', 'running', 'stopping'}
DATE_MODES = ('С выбранной даты', 'Диапазон', 'Один день')
PHASES = {
    'idle': 'Поиск выключен',
    'starting': 'Запускается…',
    'running': 'Поиск идёт',
    'stopping': 'Останавливается…',
    'stopped': 'Поиск остановлен',
    'booked': 'Приём подтверждён',
    'failed': 'Ошибка запуска',
    'interrupted': 'Поиск прервался',
}
TITLES = {'district': 'район', 'clinic': 'поликлинику', 'specialty': 'специальность'}
EVENT_LABELS = {'OK': 'Подтверждение', 'WARN': 'Предупреждение', 'ERR': 'Ошибка', 'SLOT': 'Свободный талон'}


def parse_date(text: str) -> date:
    text = text.strip()
    if '.' in text:
        return datetime.strptime(text, '%d.%m.%Y').date()
    return date.fromisoformat(text)


class DesktopApp:
    def __init__(self, root: tk.Tk, *, load_catalog: bool = True):
        self.root = root
        self.config = _read_json(CONFIG_PATH, default={})
        self.profile = load_profile() or {}
        target = next(iter(self.config.get('targets', [])), {})
        self.saved_target = target
        self.selection = {
            'district': {'id': target['district_id'], 'name': target.get('district_name', '')}
            if target.get('district_id') else None,
            'clinic': {'id': target['lpu_id'], 'name': target.get('lpu_name', ''),
                       'phone': target.get('lpu_phone')}
            if target.get('lpu_id') else None,
            'specialty': {'id': target['specialty_ids'][0], 'name': target.get('specialty_label', '')}
            if target.get('specialty_ids') else None,
        }
        self.choices = {}
        self.combos = {}
        self.controls = []
        self.tasks = queue.SimpleQueue()
        self.catalog_generation = 0
        self.catalog_level = 'district'
        self.events = None
        self.current_phase = None
        self.root.title('Талоны Горздрава')
        self.root.geometry('790x652')
        self.root.minsize(640, 570)
        self.root.configure(background=BG)
        self._styles()
        self._layout()
        self._refresh()
        if load_catalog and self.current_phase not in ACTIVE:
            self._load('district')

    def _styles(self):
        ui_font = font.nametofont('TkDefaultFont')
        ui_font.configure(size=13)
        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('.', font='TkDefaultFont', background=BG, foreground=INK)
        style.configure('TLabel', background=SURFACE, foreground=INK, padding=(0, 0, 0, 4))
        style.configure('TFrame', background=BG)
        style.configure('Panel.TFrame', background=SURFACE)
        style.configure('Title.TLabel', background=BG, font=(ui_font.actual('family'), 25, 'bold'))
        style.configure('Section.TLabel', font=(ui_font.actual('family'), 17, 'bold'))
        style.configure('Status.TLabel', background=BG, foreground=ACCENT,
                        font=(ui_font.actual('family'), 14, 'bold'), padding=0)
        style.configure('Hint.TLabel', foreground=MUTED)
        style.configure('Footer.TLabel', background=BG, foreground=MUTED, padding=0)
        style.configure('Error.TLabel', background=BG, foreground=ERROR, padding=0)
        style.configure('TNotebook', borderwidth=0)
        style.configure('TNotebook.Tab', padding=(16, 9), background='#E7EFEB')
        style.map('TNotebook.Tab', background=[('selected', SURFACE), ('active', '#DCEAE4')])
        style.configure('TEntry', padding=(10, 8), fieldbackground=SURFACE, bordercolor=BORDER)
        style.configure('TCombobox', padding=(9, 7), fieldbackground=SURFACE, bordercolor=BORDER)
        style.map('TCombobox', fieldbackground=[('readonly', SURFACE), ('disabled', BG)],
                  foreground=[('disabled', MUTED)])
        for element in ('TEntry', 'TCombobox'):
            style.map(element, bordercolor=[('focus', ACCENT)])
        style.configure('TCheckbutton', background=SURFACE, padding=(0, 6))
        style.configure('TButton', padding=(14, 9))
        style.configure('Action.TButton', background=ACCENT, foreground=SURFACE,
                        bordercolor=ACCENT, padding=(22, 11))
        style.map('Action.TButton', background=[('disabled', '#DCE6E2'), ('active', '#0E513F')],
                  foreground=[('disabled', MUTED), ('!disabled', SURFACE)],
                  bordercolor=[('focus', INK)])

    def _layout(self):
        shell = ttk.Frame(self.root, padding=(25, 18))
        shell.pack(fill='both', expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(2, weight=1)
        ttk.Label(shell, text='Талоны Горздрава', style='Title.TLabel').grid(row=0, column=0, sticky='w')
        self.phase_label = ttk.Label(shell, style='Status.TLabel')
        self.phase_label.grid(row=0, column=1, sticky='e')
        ttk.Label(shell, text='Санкт-Петербург', style='Footer.TLabel').grid(row=1, column=0, sticky='w', pady=(0, 14))
        self.last_poll = ttk.Label(shell, style='Footer.TLabel')
        self.last_poll.grid(row=1, column=1, sticky='e', pady=(0, 14))
        self.tabs = ttk.Notebook(shell)
        self.tabs.grid(row=2, column=0, columnspan=2, sticky='nsew')
        self.tabs.enable_traversal()
        self.search_tab = self._tab('Поиск')
        self.patient_tab = self._tab('Пациент')
        self.telegram_tab = self._tab('Telegram')
        self.history_tab = self._tab('История')
        self._search_form()
        self._patient_form()
        self._telegram_form()
        self._history()

        footer = ttk.Frame(shell)
        footer.grid(row=3, column=0, columnspan=2, sticky='ew', pady=(16, 0))
        footer.columnconfigure(0, weight=1)
        self.feedback = ttk.Label(footer, style='Footer.TLabel', wraplength=400)
        self.feedback.grid(row=0, column=0, sticky='w', padx=(0, 12))
        self.start = ttk.Button(footer, text='Запустить поиск', command=self._start, style='Action.TButton')
        self.start.grid(row=0, column=1, padx=(0, 8))
        self.stop = ttk.Button(footer, text='Остановить', command=self._stop)
        self.stop.grid(row=0, column=2)

    def _tab(self, text: str) -> ttk.Frame:
        frame = ttk.Frame(self.tabs, style='Panel.TFrame', padding=(23, 18))
        self.tabs.add(frame, text=text)
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)
        return frame

    def _entry(self, parent, row, column, label, value='', *, secret=False):
        var = tk.StringVar(value=value)
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky='w')
        entry = ttk.Entry(parent, textvariable=var, show='•' if secret else '')
        entry.grid(row=row + 1, column=column, sticky='ew', padx=(0, 12), pady=(0, 12))
        self.controls.append(entry)
        return var, entry

    def _search_form(self):
        panel = self.search_tab
        ttk.Label(panel, text='Поликлиника и даты', style='Section.TLabel').grid(
            row=0, column=0, columnspan=2, sticky='w', pady=(0, 7))
        for key, label, row, column, span in (
            ('district', 'Район', 1, 0, 1), ('clinic', 'Поликлиника', 1, 1, 1),
            ('specialty', 'Специальность', 3, 0, 2),
        ):
            ttk.Label(panel, text=label).grid(row=row, column=column, sticky='w')
            combo = ttk.Combobox(panel, state='readonly', height=11, width=18)
            combo.grid(row=row + 1, column=column, columnspan=span, sticky='ew', padx=(0, 12), pady=(0, 11))
            combo.bind('<<ComboboxSelected>>', lambda _, item=key: self._select(item))
            if self.selection[key]:
                combo.set(self.selection[key]['name'])
            self.combos[key] = combo
        ttk.Label(panel, text='Когда искать').grid(row=5, column=0, sticky='w')
        self.date_mode = tk.StringVar(value=(
            DATE_MODES[2] if self.saved_target.get('book_date_from') == self.saved_target.get('book_date_to')
            and self.saved_target else DATE_MODES[1] if self.saved_target.get('book_date_to') else DATE_MODES[0]
        ))
        self.mode = ttk.Combobox(panel, state='readonly', values=DATE_MODES, textvariable=self.date_mode)
        self.mode.grid(row=6, column=0, columnspan=2, sticky='ew', padx=(0, 12), pady=(0, 11))
        self.mode.bind('<<ComboboxSelected>>', lambda _: self._dates())
        start = self.saved_target.get('book_date_from', date.today().isoformat())
        end = self.saved_target.get('book_date_to') or ''
        self.date_start, self.from_entry = self._entry(panel, 7, 0, 'Дата (ДД.ММ.ГГГГ)', parse_date(start).strftime('%d.%m.%Y'))
        self.date_end, self.to_entry = self._entry(panel, 7, 1, 'По (ДД.ММ.ГГГГ)', parse_date(end).strftime('%d.%m.%Y') if end else '')
        self.to_label = panel.grid_slaves(row=7, column=1)[0]
        self._dates()
        self.auto_book = tk.BooleanVar(value=bool(self.saved_target.get('auto_book')))
        self.auto_box = ttk.Checkbutton(panel, text='Записывать автоматически', variable=self.auto_book)
        self.auto_box.grid(row=9, column=0, sticky='w')
        self.controls.append(self.auto_box)
        self.catalog_status = ttk.Label(panel, style='Hint.TLabel', wraplength=340)
        self.catalog_status.grid(row=10, column=0, columnspan=2, sticky='w', pady=(5, 0))
        self.retry = ttk.Button(panel, text='Обновить список', command=lambda: self._load(self.catalog_level))
        self.retry.grid(row=9, column=1, sticky='e', padx=(0, 12))

    def _dates(self):
        for widget in (self.to_entry, self.to_label):
            if self.date_mode.get() == DATE_MODES[1]:
                widget.grid()
            else:
                widget.grid_remove()

    def _patient_form(self):
        panel = self.patient_tab
        ttk.Label(panel, text='Данные пациента', style='Section.TLabel').grid(
            row=0, column=0, columnspan=2, sticky='w', pady=(0, 8))
        ttk.Label(panel, text='Нужны для автоматической записи.', style='Hint.TLabel').grid(
            row=1, column=0, columnspan=2, sticky='w', pady=(0, 12))
        self.patient = {}
        for key, label, row, column in (
            ('last_name', 'Фамилия', 2, 0), ('first_name', 'Имя', 2, 1),
            ('middle_name', 'Отчество, если есть', 4, 0), ('birthdate', 'Дата рождения (ДД.ММ.ГГГГ)', 4, 1),
        ):
            self.patient[key] = self._entry(panel, row, column, label, self.profile.get(key) or '')

    def _telegram_form(self):
        panel = self.telegram_tab
        ttk.Label(panel, text='Уведомления', style='Section.TLabel').grid(
            row=0, column=0, columnspan=2, sticky='w', pady=(0, 8))
        telegram = self.config.get('telegram', {})
        self.tg_enabled = tk.BooleanVar(value=bool(telegram.get('enabled')))
        self.tg_switch = ttk.Checkbutton(panel, text='Отправлять в Telegram', variable=self.tg_enabled,
                                         command=self._notification_fields)
        self.tg_switch.grid(row=1, column=0, columnspan=2, sticky='w', pady=(0, 7))
        self.tg_token, self.token_entry = self._entry(panel, 2, 0, 'Токен бота', telegram.get('bot_token', ''), secret=True)
        self.tg_chat, self.chat_entry = self._entry(panel, 2, 1, 'ID чата', telegram.get('chat_id', ''))
        self.tg_test = ttk.Button(panel, text='Отправить тест', command=self._test_telegram)
        self.tg_test.grid(row=4, column=0, sticky='w')
        self.tg_note = ttk.Label(panel, text='События сохраняются во вкладке «История».', style='Hint.TLabel')
        self.tg_note.grid(row=5, column=0, columnspan=2, sticky='w', pady=(11, 0))
        self._notification_fields()

    def _notification_fields(self):
        active = self.current_phase in ACTIVE
        editable = 'normal' if self.tg_enabled.get() and not active else 'disabled'
        for widget in (self.token_entry, self.chat_entry, self.tg_test):
            widget.configure(state=editable)

    def _history(self):
        panel = self.history_tab
        panel.rowconfigure(1, weight=1)
        ttk.Label(panel, text='История поиска', style='Section.TLabel').grid(
            row=0, column=0, columnspan=2, sticky='w', pady=(0, 9))
        self.timeline = tk.Text(
            panel, wrap='word', background=SURFACE, foreground=INK, font='TkDefaultFont',
            padx=10, pady=8, relief='flat', highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT, state='disabled',
            width=20, height=8,
        )
        self.timeline.grid(row=1, column=0, columnspan=2, sticky='nsew')
        self.timeline.tag_configure('time', foreground=MUTED)
        scroll = ttk.Scrollbar(panel, orient='vertical', command=self.timeline.yview)
        scroll.grid(row=1, column=2, sticky='ns')
        self.timeline.configure(yscrollcommand=scroll.set)

    def _background(self, operation, callback):
        def run():
            try:
                value = operation()
            except (GorzdravError, OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
                self.tasks.put((callback, None, error))
            else:
                self.tasks.put((callback, value, None))
        threading.Thread(target=run, daemon=True).start()

    def _load(self, level):
        self.catalog_level = level
        self.catalog_generation += 1
        generation = self.catalog_generation
        district, clinic = self.selection['district'], self.selection['clinic']
        self.catalog_status.configure(text='Загружаем список…')
        self.retry.configure(state='disabled')

        def fetch():
            client = GorzdravClient(verify=build_bundle(), timeout=15,
                                    direct_interface=self.config.get('http', {}).get('direct_interface'))
            with client.s:
                if level == 'district':
                    return [{'id': x['id'], 'name': x['name']} for x in client.districts()]
                if level == 'clinic':
                    return [{'id': x['id'], 'name': x['lpuShortName'], 'phone': x.get('phone'),
                             'label': ' — '.join(filter(None, (x['lpuShortName'], x.get('address'))))}
                            for x in client.lpus(int(district['id'])) if x.get('isActive')]
                return [{'id': x['id'], 'name': x['name']}
                        for x in client.specialties(int(clinic['id']))]

        def apply(records, error):
            if generation != self.catalog_generation:
                return
            self.retry.configure(state='normal')
            if error:
                self.catalog_status.configure(text='Горздрав недоступен. Попробуй обновить список.')
                return
            options = sorted(records, key=lambda row: row['name'].casefold())
            self.choices[level] = options
            combo = self.combos[level]
            combo.configure(values=[x.get('label', x['name']) for x in options])
            saved = self.selection[level]
            for index, item in enumerate(options):
                if saved and str(saved['id']) == str(item['id']):
                    combo.current(index)
                    self.selection[level] = item
                    if level == 'district':
                        self._load('clinic')
                    elif level == 'clinic':
                        self._load('specialty')
                    else:
                        self.catalog_status.configure(text='')
                    return
            self.catalog_status.configure(
                text=f'Выбери {TITLES[level]}.' if options else 'Горздрав вернул пустой список.')

        self._background(fetch, apply)

    def _select(self, level):
        self.selection[level] = self.choices[level][self.combos[level].current()]
        if level == 'specialty':
            self.catalog_status.configure(text='')
            return
        next_level = 'clinic' if level == 'district' else 'specialty'
        for key in (('clinic', 'specialty') if level == 'district' else ('specialty',)):
            self.selection[key] = None
            self.combos[key].set('')
            self.combos[key].configure(values=())
        self._load(next_level)

    def _show_error(self, text, tab, field=None):
        self.tabs.select(tab)
        self.feedback.configure(text=text, style='Error.TLabel')
        if field:
            field.focus_set()

    def _telegram_settings(self):
        token = self.tg_token.get().strip()
        chat = self.tg_chat.get().strip()
        if self.tg_enabled.get():
            if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', token):
                self._show_error('Введи токен, который выдал BotFather.', self.telegram_tab, self.token_entry)
                return None
            if not re.fullmatch(r'-?[1-9]\d*', chat):
                self._show_error('Введи числовой ID чата.', self.telegram_tab, self.chat_entry)
                return None
        return {'enabled': self.tg_enabled.get(), 'bot_token': token, 'chat_id': chat}

    def _test_telegram(self):
        telegram = self._telegram_settings()
        if telegram is None:
            return
        self.tg_test.configure(state='disabled')
        self.tg_note.configure(text='Отправляем тестовое сообщение…')

        def done(ok, error):
            self._notification_fields()
            self.tg_note.configure(text='Сообщение отправлено.' if ok and not error
                                   else 'Отправка не удалась. Проверь токен, чат и подключение.')

        self._background(
            lambda: Notifier({'telegram': telegram}).telegram('Талоны Горздрава. Проверка уведомлений.'),
            done,
        )

    def _start(self):
        for key in ('district', 'clinic', 'specialty'):
            if not self.selection[key]:
                self._show_error(f'Выбери {TITLES[key]}.', self.search_tab, self.combos[key])
                return
        try:
            start = parse_date(self.date_start.get())
            if start < date.today():
                raise ValueError
        except ValueError:
            self._show_error('Введи сегодняшнюю или будущую дату.', self.search_tab, self.from_entry)
            return
        mode = self.date_mode.get()
        end = start if mode == DATE_MODES[2] else None
        if mode == DATE_MODES[1]:
            try:
                end = parse_date(self.date_end.get())
                if end < start:
                    raise ValueError
            except ValueError:
                self._show_error('Конечная дата должна быть не раньше начальной.', self.search_tab, self.to_entry)
                return
        telegram = self._telegram_settings()
        if telegram is None:
            return
        profile = {key: var.get().strip() for key, (var, _) in self.patient.items()}
        auto = self.auto_book.get()
        if auto:
            for key, label in (('last_name', 'фамилию'), ('first_name', 'имя')):
                if not profile[key]:
                    self._show_error(f'Введи {label} пациента.', self.patient_tab, self.patient[key][1])
                    return
            try:
                birthday = parse_date(profile['birthdate'])
                if birthday > date.today():
                    raise ValueError
                profile['birthdate'] = birthday.strftime('%d.%m.%Y')
            except ValueError:
                self._show_error('Введи дату рождения в формате ДД.ММ.ГГГГ.', self.patient_tab,
                                 self.patient['birthdate'][1])
                return
            if not messagebox.askyesno(
                'Автоматическая запись',
                'Разрешить запись на ближайший подходящий талон от имени указанного пациента?',
                parent=self.root,
            ):
                return
        profile['consent_booking'] = auto
        if all(profile[key] == (self.profile.get(key) or '') for key in self.patient):
            profile['patient_ids'] = self.profile.get('patient_ids', {})
        district, clinic, specialty = (self.selection[k] for k in ('district', 'clinic', 'specialty'))
        target = {
            'district_id': district['id'], 'district_name': district['name'],
            'lpu_id': clinic['id'], 'lpu_name': clinic['name'], 'lpu_phone': clinic.get('phone'),
            'specialty_ids': [str(specialty['id'])], 'specialty_label': specialty['name'],
            'book_date_from': start.isoformat(), 'book_date_to': end.isoformat() if end else None,
            'auto_book': auto, 'notify_out_of_window': False,
        }
        config = {'targets': [target], 'auto_book': {'enabled': auto},
                  'telegram': telegram, 'http': self.config.get('http', {})}
        try:
            start_search(config, profile)
        except (BlockingIOError, OSError, ValueError) as error:
            self._show_error(str(error), self.search_tab)
        else:
            self.profile = profile
            self.config = config
            self._refresh_status()

    def _stop(self):
        try:
            request_stop()
        except OSError:
            self.feedback.configure(text='Не удалось остановить поиск. Попробуй снова.', style='Error.TLabel')
        else:
            self._refresh_status()

    def _refresh(self):
        while not self.tasks.empty():
            callback, result, error = self.tasks.get_nowait()
            callback(result, error)
        self._refresh_status()
        self.root.after(750, self._refresh)

    def _refresh_status(self):
        phase = read_status()['phase']
        active = phase in ACTIVE
        if phase != self.current_phase:
            was_active = self.current_phase in ACTIVE
            self.current_phase = phase
            self.phase_label.configure(text=PHASES.get(phase, PHASES['idle']))
            self.start.configure(state='disabled' if active else 'normal')
            self.stop.configure(state='normal' if phase in ('starting', 'running') else 'disabled')
            self.mode.configure(state='disabled' if active else 'readonly')
            self.tg_switch.configure(state='disabled' if active else 'normal')
            for combo in self.combos.values():
                combo.configure(state='disabled' if active else 'readonly')
            for control in self.controls:
                control.configure(state='disabled' if active else 'normal')
            self._notification_fields()
            if active:
                self.retry.configure(state='disabled')
            elif was_active:
                self._load('district')
            self.feedback.configure(text={
                'starting': 'Готовим фоновый поиск.',
                'running': 'Поиск продолжится после закрытия окна.',
                'stopping': 'Завершаем текущую проверку.',
                'interrupted': 'Можно запустить поиск снова.',
                'failed': 'Подробнее в журнале desktop.log.',
            }.get(phase, ''), style='Footer.TLabel')

        last = load_state()['last_scan']
        poll_label = f'Проверено в {last[11:16]}' if last else 'Проверок пока нет'
        self.last_poll.configure(text=poll_label)
        events = _read_json(EVENTS, default=[])
        if self.events == events:
            return
        self.events = events
        self.timeline.configure(state='normal')
        self.timeline.delete('1.0', 'end')
        if events:
            for event in reversed(events):
                stamp = event['time'].replace('T', ' ')[:16]
                label = EVENT_LABELS.get(event['kind'], 'Событие')
                self.timeline.insert('end', f'{stamp}  ·  {label}\n', 'time')
                self.timeline.insert('end', f'{event["text"]}\n\n')
        else:
            self.timeline.insert('end', 'Здесь появятся свободные талоны и результаты записи.')
        self.timeline.configure(state='disabled')
