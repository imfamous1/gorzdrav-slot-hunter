from datetime import datetime, timedelta
from unittest import TestCase
from unittest.mock import MagicMock

from gorzdrav_hunter.client import GorzdravError
from gorzdrav_hunter.desktop_visits import PETERSBURG, cancel_visit, fetch_visits, visit_scopes


class DesktopVisitTests(TestCase):
    def setUp(self):
        future = (datetime.now(PETERSBURG) + timedelta(days=2)).replace(microsecond=0)
        self.record = {'lpuId': '1', 'patientId': 'first-card', 'appointmentId': 'visit',
                       'visitStart': future.isoformat()}
        self.profile = {'first_name': 'Анна', 'last_name': 'Примерова', 'birthdate': '14.02.1990',
                        'patient_ids': {'1': 'first-card', '2': 'second-card'}}
        self.client = MagicMock()

    def test_fetches_known_clinics_once_and_keeps_partial_results(self):
        job = {'patient': 'person', 'target': {'lpu_id': 1, 'lpu_name': 'Пробная поликлиника'}}
        scopes = visit_scopes({'patients': {'person': self.profile}, 'jobs': {'one': job, 'two': job}})
        self.client.patient_appointments.side_effect = [[self.record], GorzdravError(615)]
        results = fetch_visits(self.client, scopes)
        self.assertEqual(2, self.client.patient_appointments.call_count)
        self.client.patient_search.assert_not_called()
        self.assertEqual('Пробная поликлиника', results['person', '1'][0].clinic)
        self.assertIsNone(results['person', '2'])

    def test_does_not_cancel_reused_id_at_another_time(self):
        scope = ('person', '1')
        self.client.patient_appointments.return_value = [self.record]
        visit, = fetch_visits(self.client, {scope: (self.profile, 'Клиника')})[scope]
        changed = {**self.record, 'visitStart': (visit.start + timedelta(hours=1)).isoformat()}
        self.client.patient_appointments.return_value = [changed]

        self.assertEqual('absent', cancel_visit(self.client, visit, self.profile))
        self.client.appointment_cancel.assert_not_called()

    def test_timeout_does_not_repeat_cancel_or_claim_success(self):
        scope = ('person', '1')
        self.client.patient_appointments.return_value = [self.record]
        visit, = fetch_visits(self.client, {scope: (self.profile, 'Клиника')})[scope]
        self.client.appointment_cancel.side_effect = GorzdravError(-1, 'timeout')

        self.assertEqual('unverified', cancel_visit(self.client, visit, self.profile))
        self.client.appointment_cancel.assert_called_once()
