from unittest import TestCase
from unittest.mock import Mock, patch

from gorzdrav_hunter.desktop_plan import FIRST_JOB, apply_plan, read_plan, task_states
from gorzdrav_hunter.desktop_worker import DesktopSearchGroup

TARGET = {
    'lpu_id': 30, 'lpu_name': 'Тестовая поликлиника', 'specialty_ids': ['eye'],
    'doctor_ids': ['one'], 'book_date_from': '2031-02-10', 'auto_book': True,
}
ORIGINAL = {
    'appointment_id': 'fixture', 'specialty_id': 'eye', 'visit_start': '2031-02-02T08:00:00',
}


class DesktopPlanTests(TestCase):
    def test_same_appointment_cannot_have_two_transfers(self):
        plan = {
            'patients': {'a': {}},
            'jobs': {
                key: {'target': dict(TARGET, reschedule_from=ORIGINAL), 'patient': 'a', 'consent': True}
                for key in ('first', 'second')
            },
        }
        with self.assertRaisesRegex(ValueError, 'один автоперенос'):
            DesktopSearchGroup({'targets': [TARGET]}, plan, {}, Mock(), lambda: False)

    def test_import_preserves_pending_transfer_history_and_profile(self):
        profile = {'last_name': 'Тестов', 'first_name': 'Тест', 'consent_booking': True}
        config = {'targets': [dict(TARGET, reschedule_from=ORIGINAL)]}
        state = {'booked': [{'slot_id': 'fixture'}], 'reschedules': {'fixture': {'status': 'submitted'}}}
        plan = read_plan(config, profile)
        migrated, = plan['jobs'].values()
        self.assertTrue(migrated['consent'])
        self.assertEqual(ORIGINAL, migrated['target']['reschedule_from'])
        self.assertEqual(profile, plan['patients'][migrated['patient']])
        job_state = task_states(state, plan)[FIRST_JOB]
        self.assertEqual('submitted', job_state['reschedules']['fixture']['status'])
        self.assertEqual(state['booked'], job_state['booked'])
        self.assertIsNot(state['booked'], job_state['booked'])
        self.assertEqual(plan, read_plan(apply_plan(config, plan), None))

    def test_patients_and_doctors_keep_separate_sessions_and_slot_state(self):
        plan = {
            'patients': {'a': {'first_name': 'Иван'}, 'b': {'first_name': 'Анна'}},
            'jobs': {
                'a1': {'target': TARGET, 'patient': 'a', 'consent': True},
                'b1': {'target': TARGET, 'patient': 'b', 'consent': False},
                'a2': {'target': dict(TARGET, doctor_ids=['two']), 'patient': 'a', 'consent': True},
            },
        }
        cfg = {'http': {'sleep_between_requests_sec': 1}, 'targets': [TARGET]}
        state = {'booked': [], 'reschedules': {}}
        clients = [Mock() for _ in range(3)]
        group = DesktopSearchGroup(cfg, plan, state, Mock(side_effect=clients), lambda: False)
        hunters = [hunter for _, hunter in group.jobs]
        self.assertEqual(clients, [hunter.client for hunter in hunters])
        self.assertEqual(['Иван', 'Анна', 'Иван'],
                         [hunter.booker.profile['first_name'] for hunter in hunters])
        self.assertEqual([True, False, True], [h.booker.profile['consent_booking'] for h in hunters])
        self.assertEqual(['two'], hunters[2].cfg['targets'][0]['doctor_ids'])

        def finish(job_state, **kwargs):
            job_state['booked'].append({'slot_id': 'consultation'})
            job_state['seen']['30'] = {'one:slot': '2031-02-10T09:00:00'}
            return True

        hunters[0].scan_once = Mock(side_effect=finish)
        hunters[1].scan_once = Mock(return_value=False)
        hunters[2].scan_once = Mock(return_value=False)
        with patch('gorzdrav_hunter.desktop_worker.save_state'), patch('gorzdrav_hunter.desktop_worker.time.sleep'):
            self.assertFalse(group.scan_once(state))
            self.assertFalse(group.scan_once(state))
        hunters[0].scan_once.assert_called_once()
        self.assertEqual(2, hunters[1].scan_once.call_count)
        self.assertEqual(2, hunters[2].scan_once.call_count)
        self.assertEqual('completed', group.states['a1']['phase'])
        self.assertEqual({}, group.states['b1']['seen'])
        self.assertEqual({}, group.states['a2']['seen'])

    def test_open_group_picks_up_new_job_without_replacing_first_hunter(self):
        first = {'target': TARGET, 'patient': 'a', 'consent': True}
        second = {
            'target': dict(TARGET, doctor_ids=['second'], auto_book=False),
            'patient': 'b', 'consent': False,
        }
        initial = {'patients': {'a': {'first_name': 'Иван'}}, 'jobs': {'first': first}}
        current = {
            'patients': {**initial['patients'], 'b': {'first_name': 'Анна'}},
            'jobs': {'first': first, 'second': second},
        }
        state = {}
        load = Mock(return_value=initial)
        finish = Mock()
        group = DesktopSearchGroup(
            {'http': {'sleep_between_requests_sec': 1}, 'targets': [TARGET]},
            initial, state, Mock(side_effect=[Mock(), Mock()]), lambda: False,
            load_plan=load, finish=finish,
        )
        _, original = group.jobs[0]

        def finish_original(job_state, **_):
            job_state['booked'].append({'slot_id': 'fixture'})
            load.return_value = current
            return True

        original.scan_once = Mock(side_effect=finish_original)
        with patch('gorzdrav_hunter.desktop_worker.save_state'):
            self.assertFalse(group.scan_once(state))
            finish.assert_not_called()
            self.assertIs(original, group.jobs[0][1])
            self.assertEqual(['first', 'second'], [key for key, _ in group.jobs])
            self.assertEqual('completed', group.states['first']['phase'])
            self.assertEqual([{'slot_id': 'fixture'}], group.states['first']['booked'])
            self.assertIsNot(original.client, group.jobs[1][1].client)
            group.jobs[1][1].scan_once = Mock(return_value=False)
            self.assertFalse(group.scan_once(state))
        original.scan_once.assert_called_once()
        group.jobs[1][1].scan_once.assert_called_once()
