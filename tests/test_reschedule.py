from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

from gorzdrav_hunter.client import GorzdravClient, GorzdravError, Slot
from gorzdrav_hunter.config import normalize_target
from gorzdrav_hunter.hunter import Hunter
from gorzdrav_hunter.reschedule import Rescheduler


SOURCE_ID = 'fixture-old-appointment'
SOURCE_START = '2031-05-02T09:15:00'
TARGET = normalize_target({
    'lpu_id': 99, 'lpu_name': 'Тестовая поликлиника',
    'specialty_ids': ['eye', 'other'], 'auto_book': True,
    'book_date_from': '2031-05-10',
    'reschedule_from': {
        'appointment_id': SOURCE_ID, 'specialty_id': 'eye', 'visit_start': SOURCE_START,
    },
}, {})
DESTINATION = Slot(
    lpu_id=99, specialty_id='eye', specialty_name='Офтальмолог',
    doctor_id='doctor-b', doctor_name='Врач Б', id='fixture-new-slot',
    visit_start='2031-05-10T14:00:00', visit_end='2031-05-10T14:15:00',
    room=None, number=None, address=None,
)


def record(appointment_id, visit_start, doctor_id, specialty_id='eye'):
    return {
        'appointmentId': appointment_id, 'lpuId': '99',
        'visitStart': visit_start,
        'specialityRendingConsultation': {'id': specialty_id},
        'doctorRendingConsultation': {'id': doctor_id},
        'isAppointmentByReferral': False, 'referralId': None,
    }


class RescheduleTests(unittest.TestCase):
    def setUp(self):
        self.state = {'seen': {}, 'booked': [], 'booked_ids': [], 'reschedules': {}}
        self.old = record(SOURCE_ID, SOURCE_START, 'doctor-a')
        self.new = record('confirmed-new', DESTINATION.visit_start, DESTINATION.doctor_id)
        self.client = Mock(spec=GorzdravClient)
        self.booker = Mock(profile={'consent_booking': True}, should_stop=None)
        self.booker.patient_id.return_value = 'fixture-patient'
        self.booker.card.return_value = 'Карточка талона'
        self.notifier = Mock()
        self.rescheduler = Rescheduler(self.client, self.booker, self.notifier)
        self.saved = []
        patcher = patch('gorzdrav_hunter.reschedule.save_state',
                        side_effect=lambda state: self.saved.append(deepcopy(state)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.client.appointment_cancel.assert_not_called()
        self.client.appointment_create.assert_not_called()

    def test_transfers_with_one_request_and_checks_new_and_old(self):
        self.client.patient_appointments.side_effect = [[self.old], [self.new]]

        def assert_saved_before_request(**kwargs):
            self.assertEqual('submitted', self.saved[-1]['reschedules'][SOURCE_ID]['status'])
            self.notifier.broadcast.assert_not_called()

        self.client.appointment_change.side_effect = assert_saved_before_request
        self.assertTrue(self.rescheduler.submit(self.state, TARGET, DESTINATION, dry_run=False))
        self.client.appointment_change.assert_called_once_with(
            lpu_id=99, patient_id='fixture-patient', previous_id=SOURCE_ID, slot_id=DESTINATION.id,
        )
        self.assertEqual('completed', self.saved[-1]['reschedules'][SOURCE_ID]['status'])
        self.assertEqual(DESTINATION.visit_start, self.state['booked'][0]['visit_start'])
        self.notifier.broadcast.assert_called_once_with(
            '✅ ПРИЁМ ПЕРЕНЕСЁН\nКарточка талона', kind='OK',
        )

    def test_missing_source_blocks_transfer(self):
        self.client.patient_appointments.return_value = [
            record('some-other-appointment', SOURCE_START, 'doctor-a'),
        ]
        self.assertTrue(self.rescheduler.submit(self.state, TARGET, DESTINATION, dry_run=False))
        self.client.appointment_change.assert_not_called()
        self.assertEqual('blocked', self.state['reschedules'][SOURCE_ID]['status'])

    def test_timeout_keeps_pending_and_reconciles_after_restart(self):
        self.client.appointment_change.side_effect = GorzdravError(-1, 'network timeout')
        self.client.patient_appointments.side_effect = [[self.old], [self.old], [self.new]]
        with self.assertLogs('gorzdrav_hunter.reschedule', 'WARNING'):
            self.assertFalse(self.rescheduler.submit(self.state, TARGET, DESTINATION, dry_run=False))
        restored = deepcopy(self.saved[-1])
        restarted = Rescheduler(self.client, self.booker, self.notifier)
        self.assertTrue(restarted.reconcile(restored, TARGET, dry_run=False))
        self.assertEqual('completed', restored['reschedules'][SOURCE_ID]['status'])
        self.client.appointment_change.assert_called_once()

    def test_disappeared_slot_preserves_original_and_allows_next_candidate(self):
        self.client.appointment_change.side_effect = GorzdravError(645, 'slot gone')
        self.client.patient_appointments.side_effect = [[self.old], [self.old]]
        with self.assertLogs('gorzdrav_hunter.reschedule', 'WARNING'):
            self.assertFalse(self.rescheduler.submit(self.state, TARGET, DESTINATION, dry_run=False))
        self.assertNotIn(SOURCE_ID, self.state['reschedules'])
        self.assertIn(f'{DESTINATION.doctor_id}:{DESTINATION.id}', self.state['seen']['99'])
        self.assertEqual([], self.state['booked'])
        self.notifier.broadcast.assert_not_called()

    def test_dry_run_selects_earliest_in_same_specialty(self):
        target = {**TARGET, 'book_time_from': '13:00', 'book_time_to': '15:00'}
        hunter = Hunter(self.client, {'targets': [target]}, self.notifier, self.booker)
        hunter.collect_slots = Mock(return_value=[
            replace(DESTINATION, visit_start='2031-05-09T14:00:00', id='too-early'),
            replace(DESTINATION, visit_start='2031-05-10T08:00:00', id='outside-hours'),
            replace(DESTINATION, visit_start='2031-05-10T13:00:00', id='other', specialty_id='other'),
            replace(DESTINATION, visit_start='2031-05-12T14:00:00', id='later'),
            DESTINATION,
        ])
        self.assertFalse(hunter.scan_once(self.state, dry_run=True, persist=False))
        self.booker.card.assert_called_once_with(target, DESTINATION)
        self.client.patient_appointments.assert_not_called()
        self.client.appointment_change.assert_not_called()
        self.assertEqual([], self.saved)

    def test_transfer_client_matches_portal_request(self):
        client = GorzdravClient()
        with patch.object(client, '_call') as call:
            client.appointment_change(
                lpu_id=99, patient_id='fixture-patient', previous_id=SOURCE_ID, slot_id=DESTINATION.id,
            )
        call.assert_called_once_with('POST', '/appointment/change', json_body={
            'esiaId': None, 'appointmentId': DESTINATION.id,
            'previousAppointmentId': SOURCE_ID, 'lpuId': '99',
            'patientId': 'fixture-patient', 'referralId': None,
        })
