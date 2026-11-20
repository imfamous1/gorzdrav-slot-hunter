from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from unittest.mock import MagicMock, patch

from gorzdrav_hunter.client import GorzdravError


@skipUnless(find_spec('PySide6'), 'desktop dependencies are not installed')
class DesktopSessionsTests(TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtCore import QCoreApplication

        cls.qt = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        from gorzdrav_hunter import desktop, desktop_worker
        from gorzdrav_hunter.config import _read_json, _write_json
        from gorzdrav_hunter.desktop_plan import apply_plan

        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.desktop = desktop
        self.read_json = _read_json
        self.write_json = _write_json
        self.config_path = root / 'config.json'
        self.state_path = root / 'state.json'
        self.status = {'phase': 'running', 'per_job': True, 'id': 'run'}
        target = {'lpu_id': 1, 'district_id': 1, 'specialty_ids': ['eye'],
                  'book_date_from': '2031-05-10', 'auto_book': True}
        self.jobs = {
            'legacy': {'target': {**target, 'auto_book': False}},
            'watching': {'target': target, 'patient': 'test', 'run': {'worker': 'run', 'token': 'one'}},
            'stopped': {'target': {**target, 'lpu_id': 2}, 'patient': 'test', 'revision': 'kept',
                        'run': {'worker': 'run', 'token': 'two', 'stop': True}},
            'pending': {'target': {**target, 'lpu_id': 3, 'reschedule_from': {
                'appointment_id': 'source', 'specialty_id': 'eye', 'visit_start': '2031-05-01T09:00:00',
            }}},
        }
        self.state = {'desktop_jobs': {
            'watching': {'phase': 'watching', 'run_token': 'one'},
            'stopped': {'phase': 'stopped', 'run_token': 'two', 'revision': 'kept'},
            'pending': {'reschedules': {'source': {'status': 'submitted'}}},
        }}
        _write_json(self.config_path, apply_plan({}, {
            'patients': {'test': {'first_name': 'Проба', 'last_name': 'Тестов', 'birthdate': '14.02.1990',
                                  'patient_ids': {'2': 'old-card'}}},
            'jobs': self.jobs,
        }))
        _write_json(self.state_path, self.state)
        for module in (desktop, desktop_worker):
            mock = patch.multiple(module, CONFIG_PATH=self.config_path, CONTROL_LOCK=root / 'desktop.lock',
                                  load_profile=lambda: None, load_state=lambda: _read_json(self.state_path),
                                  read_status=lambda: self.status)
            mock.start()
            self.addCleanup(mock.stop)

    def open_session(self):
        window = self.desktop.DesktopApp(load_catalog=False)
        self.addCleanup(lambda: window.timer.stop())
        return window

    def test_reopens_active_and_pending_tabs_but_drops_closed_tabs_and_drafts(self):
        first = self.open_session()
        self.assertEqual(['watching', 'pending'], [tab['id'] for tab in first.view['tabs']])
        self.assertEqual('watching', first.selected)
        first.newSearch()
        draft = first.selected
        self.assertIn(draft, [tab['id'] for tab in first.view['tabs']])

        self.jobs['watching']['run']['stop'] = True
        config = self.read_json(self.config_path)
        config['desktop_plan']['jobs'] = self.jobs
        self.write_json(self.config_path, config)
        closing = self.open_session()
        self.assertEqual(['pending'], [tab['id'] for tab in closing.view['tabs']])
        self.state['desktop_jobs']['watching']['phase'] = 'stopped'
        self.write_json(self.state_path, self.state)
        first._refresh()
        self.assertIn('watching', [tab['id'] for tab in first.view['tabs']])
        reopened = self.open_session()
        self.assertEqual(['pending'], [tab['id'] for tab in reopened.view['tabs']])

    def test_confirming_same_search_reuses_the_hidden_job(self):
        controller = self.open_session()
        controller.newSearch()
        draft = controller.selected
        controller.forms[draft] = self.desktop.SearchForm.from_job(self.jobs['stopped'], 'test')
        controller.searchAction()
        controller.confirmSearch(False)
        self.assertEqual(draft, controller.selected)
        self.assertEqual(self.jobs, self.read_json(self.config_path)['desktop_plan']['jobs'])

        client = MagicMock()
        client.patient_search.return_value = 'fresh-card'
        with patch.object(controller, '_client', return_value=client), patch.object(controller, '_background') as queue:
            controller.searchAction()
            controller.confirmSearch(True)
            self.assertEqual('checking_card', controller.view['phase'])
            self.assertEqual(self.jobs, self.read_json(self.config_path)['desktop_plan']['jobs'])
            check, done = queue.call_args.args
            done(check(), None)
        client.patient_search.assert_called_once_with(2, 'Тестов', 'Проба', '', '1990-02-14T00:00:00', '14.02.1990')
        plan = self.read_json(self.config_path)['desktop_plan']
        saved = plan['jobs']
        self.assertEqual('stopped', controller.selected)
        self.assertEqual('kept', saved['stopped']['revision'])
        self.assertNotIn(draft, saved)
        self.assertEqual(self.jobs['watching'], saved['watching'])
        self.assertEqual({'2': 'fresh-card'}, plan['patients']['test']['patient_ids'])
        self.assertEqual(self.state, self.read_json(self.state_path))

    def test_missing_card_and_network_error_leave_the_search_unstarted(self):
        controller = self.open_session()
        controller.newSearch()
        draft = controller.selected
        controller.forms[draft] = self.desktop.SearchForm.from_job(self.jobs['stopped'], 'test')
        before = self.read_json(self.config_path)
        with patch.object(controller, '_background') as queue:
            for error, message in (
                (None, 'Поликлиника не нашла медкарту'),
                (GorzdravError(-1), 'Не удалось проверить медкарту на Горздраве'),
            ):
                with self.subTest(error=error):
                    controller.searchAction()
                    controller.confirmSearch(True)
                    _, done = queue.call_args.args
                    done(None, error)
                    self.assertEqual('draft', controller.view['phase'])
                    self.assertIn(message, controller.view['cardError'])
                    self.assertEqual(before, self.read_json(self.config_path))

    def test_patient_birthdate_has_calendar_bounds(self):
        from datetime import date, timedelta

        self.status['phase'] = 'idle'
        controller = self.open_session()
        controller.selectPatient('')
        controller.setPatient('first_name', 'Анна')
        controller.setPatient('last_name', 'Примерова')
        today = date.today()
        next_day = today + timedelta(days=1)
        for invalid in ('29.02.2001', '11.11.1000', next_day.strftime('%d.%m.%Y')):
            with self.subTest(invalid=invalid):
                controller.setPatient('birthdate', invalid)
                controller.savePatient()
                self.assertEqual('birthdate', controller.notices['patient']['field'])
                self.assertEqual({'test'}, set(controller.plan['patients']))

        controller.setPatient('birthdate', today.strftime('%d.%m.%Y'))
        controller.savePatient()
        profile = self.read_json(self.config_path)['desktop_plan']['patients'][controller.patient_id]
        self.assertEqual(today.strftime('%d.%m.%Y'), profile['birthdate'])

    def test_search_time_is_optional_and_restored_for_saved_jobs(self):
        controller = self.open_session()
        prior = self.jobs['stopped']
        form = self.desktop.SearchForm.from_job(prior, 'test')
        self.assertFalse(form.time_limited)
        self.assertEqual((None, None), form.time_bounds())
        original_scope = self.desktop.job_scope(prior)

        saved = {**prior, 'target': {**prior['target'], 'book_time_to': '12:00'}}
        form = self.desktop.SearchForm.from_job(saved, 'test')
        self.assertTrue(form.time_limited)
        self.assertEqual((None, '12:00'), form.time_bounds())
        self.assertNotEqual(original_scope, self.desktop.job_scope(saved))
        target = form.build(saved['target'], controller.plan['patients'])['target']
        self.assertIsNone(target['book_time_from'])
        self.assertEqual('12:00', target['book_time_to'])

        controller.newSearch()
        controller.forms[controller.selected] = form
        controller.setSearch('time_from', '13:00')
        with self.assertRaises(self.desktop.FormError) as error:
            form.time_bounds()
        self.assertEqual('time_to', error.exception.field)
        controller.limitSearchTime(False)
        self.assertEqual((None, None), form.time_bounds())
