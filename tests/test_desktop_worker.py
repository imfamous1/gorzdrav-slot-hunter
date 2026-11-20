from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
import subprocess
import sys
import time
import unittest

from gorzdrav_hunter import desktop_worker as worker
from gorzdrav_hunter.booker import Booker
from gorzdrav_hunter.client import AttachmentDenied, GorzdravClient, GorzdravError
from gorzdrav_hunter.config import _read_json, _write_json
from gorzdrav_hunter.desktop_plan import apply_plan, read_plan
from gorzdrav_hunter.process_lock import is_locked, process_lock


class DesktopWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        paths = {name: self.root / name.lower() for name in (
            'STATUS', 'STOP', 'EVENTS', 'CONTROL_LOCK', 'WORKER_LOCK',
        )}
        patcher = patch.multiple(worker, **paths)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_process_liveness_is_shared_between_programs(self):
        script = 'from gorzdrav_hunter.process_lock import is_locked; from pathlib import Path; import sys; print(is_locked(Path(sys.argv[1])))'
        with process_lock(worker.WORKER_LOCK):
            output = subprocess.check_output(
                [sys.executable, '-c', script, str(worker.WORKER_LOCK)], text=True,
            )
            self.assertEqual('True', output.strip())
        self.assertFalse(is_locked(worker.WORKER_LOCK))

    def test_stop_during_start_survives_closing_interface(self):
        _write_json(worker.STATUS, {'id': 'example', 'phase': 'starting', 'started_at': time.time()})
        self.assertEqual('starting', worker.read_status()['phase'])
        worker.request_stop()
        self.assertEqual('stopping', worker.read_status()['phase'])
        self.assertEqual(0, worker.run_worker('example'))
        self.assertEqual('stopped', worker.read_status()['phase'])

    def test_stale_status_does_not_claim_search_is_running(self):
        _write_json(worker.STATUS, {'id': 'old', 'phase': 'running', 'started_at': 1})
        self.assertEqual('interrupted', worker.read_status()['phase'])

    def test_monitoring_event_remains_visible_without_telegram(self):
        worker.DesktopNotifier({'telegram': {'enabled': False}}).broadcast(
            'Свободный талон', kind='SLOT',
        )
        self.assertEqual('Свободный талон', _read_json(worker.EVENTS)[0]['text'])

    def test_stop_before_booking_does_not_submit_a_request(self):
        client = Mock()
        booker = Booker(client, None, Mock(), should_stop=lambda: True)
        result = booker._create_with_retries({}, None, {})
        self.assertEqual('declined', result)
        client.appointment_create.assert_not_called()

    def test_explicit_attachment_refusal_is_not_retried(self):
        client = GorzdravClient()
        booker = Booker(client, None, Mock())
        message = 'Пациент не прикреплён к медицинской организации.'
        with patch.object(client, '_call', side_effect=GorzdravError(1, message)) as call:
            with self.assertRaises(AttachmentDenied):
                booker._create_with_retries({}, None, {})
            call.assert_called_once()
        with patch.object(client, '_call', side_effect=GorzdravError(620, 'Проверьте прикрепление по ОМС.')):
            with self.assertRaises(GorzdravError) as error:
                client.appointment_create({})
            self.assertNotIsInstance(error.exception, AttachmentDenied)

    def test_attachment_refusal_stops_one_job_and_allows_restart(self):
        state = {}
        target = {'lpu_id': 1, 'specialty_ids': ['eye'], 'book_date_from': '2031-05-10'}
        status = {'phase': 'running', 'id': 'run', 'per_job': True}
        jobs = {key: {'target': {**target, 'lpu_id': index}, 'run': {'worker': 'run', 'token': key}}
                for index, key in enumerate(('first', 'second'), 1)}
        plan = {'patients': {}, 'jobs': jobs}
        group = worker.DesktopSearchGroup(
            {'http': {'sleep_between_requests_sec': 1}}, plan, state, Mock(), lambda: False,
            load_plan=lambda: plan, run_id='run',
        )
        first, second = dict(group.jobs).values()
        first.scan_once = Mock(side_effect=AttachmentDenied(8))
        second.scan_once = Mock(return_value=False)
        with patch.object(worker, 'save_state'), patch.object(worker.time, 'sleep'):
            group.scan_once(state)
            group.scan_once(state)
        self.assertEqual('attachment_denied', worker.job_phase(status, jobs['first'], group.states['first']))
        self.assertEqual('running', worker.job_phase(status, jobs['second'], group.states['second']))
        first.scan_once.assert_called_once()
        self.assertEqual(2, second.scan_once.call_count)
        jobs['first']['run']['token'] = 'restart'
        self.assertEqual('starting', worker.job_phase(status, jobs['first'], group.states['first']))

    def test_tabs_can_start_stop_and_restart_in_one_worker(self):
        config = self.root / 'config.json'
        state = {}
        cfg = {'http': {'sleep_between_requests_sec': 1}}
        transfer = {
            'appointment_id': 'fixture', 'specialty_id': 'eye',
            'visit_start': '2031-05-01T09:00:00',
        }
        target = {'lpu_id': 1, 'specialty_ids': ['eye'], 'book_date_from': '2031-05-10'}
        first = {'target': {**target, 'reschedule_from': transfer}, 'revision': 'first'}
        second = {'target': {**target, 'lpu_id': 2}, 'revision': 'second'}
        _write_json(config, apply_plan({}, {'patients': {}, 'jobs': {'b': second}}))

        def load_plan():
            return read_plan(_read_json(config), None)

        with patch.multiple(worker, CONFIG_PATH=config, load_state=lambda: state,
                            load_profile=lambda: None, save_state=Mock()), patch.object(
            worker.subprocess, 'Popen',
        ) as spawn, patch.object(worker.time, 'sleep'):
            worker.start_desktop_job('a', first, None)
            run_id = worker.read_status()['id']
            with process_lock(worker.WORKER_LOCK):
                group = worker.DesktopSearchGroup(
                    cfg, load_plan(), state, Mock(side_effect=[Mock(), Mock(), Mock()]),
                    lambda: False, load_plan=load_plan, run_id=run_id,
                )
                self.assertEqual(['a'], list(dict(group.jobs)))
                worker.start_desktop_job('b', second, None)
                self.assertFalse(group.settle_jobs())
                a, b = dict(group.jobs).values()
                a.scan_once = Mock(return_value=False)
                b.scan_once = Mock(return_value=False)
                self.assertFalse(group.scan_once(state))
                a.scan_once.assert_called_once()
                b.scan_once.assert_called_once()

                run = load_plan()['jobs']['a']['run']
                group.states['a']['reschedules']['fixture'] = {'status': 'submitted'}
                worker.stop_job('a', run_id, token=run['token'])
                self.assertTrue(a.booker.should_stop())
                self.assertFalse(b.booker.should_stop())
                self.assertFalse(group.settle_jobs())
                self.assertEqual('stopped', group.states['a']['phase'])
                self.assertFalse(group.scan_once(state))
                a.scan_once.assert_called_once()
                self.assertEqual(2, b.scan_once.call_count)

                with self.assertRaisesRegex(ValueError, 'дождись подтверждения'):
                    worker.start_desktop_job('a', {**first, 'revision': 'changed'}, None)
                worker.start_desktop_job('a', first, None)
                self.assertFalse(group.settle_jobs())
                self.assertIs(b, dict(group.jobs)['b'])
                self.assertIsNot(a, dict(group.jobs)['a'])
                self.assertEqual('submitted', group.states['a']['reschedules']['fixture']['status'])
                self.assertEqual('watching', group.states['b']['phase'])
            spawn.assert_called_once()

    def test_stop_before_first_scan_is_acknowledged(self):
        config = self.root / 'config.json'
        state = {}
        target = {'lpu_id': 1, 'specialty_ids': ['eye'], 'book_date_from': '2031-05-10'}
        with patch.multiple(worker, CONFIG_PATH=config, load_state=lambda: state,
                            load_profile=lambda: None, save_state=Mock()), patch.object(
            worker.subprocess, 'Popen',
        ):
            cfg = worker.start_desktop_job('a', {'target': target}, None)
            status = worker.read_status()
            run = cfg['desktop_plan']['jobs']['a']['run']
            worker.stop_job('a', status['id'], token=run['token'])
            load_plan = lambda: read_plan(_read_json(config), None)
            factory = Mock()
            group = worker.DesktopSearchGroup(
                {}, load_plan(), state, factory, lambda: False,
                load_plan=load_plan, run_id=status['id'],
            )
            self.assertTrue(group.scan_once(state))
            self.assertEqual('stopped', state['desktop_jobs']['a']['phase'])
            factory.assert_not_called()

    def test_failed_launch_does_not_save_the_draft(self):
        config = self.root / 'config.json'
        target = {'lpu_id': 1, 'specialty_ids': ['eye'], 'book_date_from': '2031-05-10'}
        existing = apply_plan({}, {'patients': {}, 'jobs': {'a': {'target': target}}})
        with patch.multiple(worker, CONFIG_PATH=config, load_state=lambda: {}), patch.object(
            worker.subprocess, 'Popen', side_effect=OSError('could not start process'),
        ):
            for initial in (None, existing):
                with self.subTest(has_saved_plan=initial is not None):
                    if initial is not None:
                        _write_json(config, initial)
                    with self.assertRaisesRegex(OSError, 'could not start'):
                        worker.start_desktop_job('draft', {'target': target}, None)
                    self.assertEqual(initial, _read_json(config))


class AppendLivePlanTests(unittest.TestCase):
    def test_append_uses_latest_disk_state_and_rejects_another_run(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            config = path / 'config.json'
            _write_json(config, {
                'desktop_plan': {
                    'patients': {'orig': {'patient_ids': {'1': 'fresh-cache'}}},
                    'jobs': {'original': {'target': {'lpu_id': 1}, 'patient': 'orig'}},
                },
            })
            task = {'target': {'lpu_id': 1, 'specialty_ids': ['fixture'],
                               'book_date_from': '2031-05-01'}, 'patient': None}
            status = {'id': 'first', 'phase': 'running', 'live_plan': True}
            with patch.object(worker, 'CONFIG_PATH', config), patch.object(
                worker, 'CONTROL_LOCK', path / 'control.lock',
            ), patch.object(worker, 'read_status', return_value=status):
                with self.assertRaises(ValueError):
                    worker.append_live_plan('second', job=('new', task))
                updated = worker.append_live_plan('first', job=('new', task))
            plan = updated['desktop_plan']
            self.assertEqual({'1': 'fresh-cache'}, plan['patients']['orig']['patient_ids'])
            self.assertEqual({'target': {'lpu_id': 1}, 'patient': 'orig'}, plan['jobs']['original'])
            self.assertEqual(task, plan['jobs']['new'])


if __name__ == '__main__':
    unittest.main()
