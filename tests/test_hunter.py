from argparse import Namespace
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from gorzdrav_hunter import cli, config
from gorzdrav_hunter.client import GorzdravError, Slot
from gorzdrav_hunter.hunter import Hunter


TARGET = {
    'lpu_id': 99,
    'lpu_name': 'Тестовая поликлиника',
    'specialty_ids': ['1'],
    'book_date_from': '2030-06-10',
    'book_date_to': None,
    'auto_book': True,
}
SLOT = Slot(
    lpu_id=99, specialty_id='1', specialty_name='Специалист',
    doctor_id='test-doctor', doctor_name='Дежурный врач', id='slot-1',
    visit_start='2030-06-10T10:00:00', visit_end='2030-06-10T10:15:00',
    room=None, number=None, address=None,
)


class HunterTests(unittest.TestCase):
    def setUp(self):
        self.target = config.normalize_target(TARGET, {})
        self.cfg = {'targets': [self.target], 'poll_interval_sec': 60}
        self.notifier = Mock()
        self.booker = Mock()
        self.booker.card.return_value = 'Карточка талона'
        self.booker.profile = {'consent_booking': True}
        self.hunter = Hunter(Mock(), self.cfg, self.notifier, self.booker)
        self.state = {
            'seen': {}, 'booked_ids': [], 'booked': [],
            'found_cache': [], 'paused': {},
        }

    def test_slots_outside_window_are_recorded_without_booking(self):
        older = replace(SLOT, visit_start='2030-06-09T10:00:00')
        self.hunter.collect_slots = Mock(return_value=[older])
        self.assertFalse(self.hunter.scan_once(self.state, persist=False))
        self.assertFalse(self.state['found_cache'][0]['in_window'])
        self.assertIn('test-doctor:slot-1', self.state['seen']['99'])
        self.booker.book.assert_not_called()
        self.assertFalse(self.hunter.scan_once(self.state, persist=False))
        self.notifier.info.assert_called_once()

    def test_date_window_handles_inclusive_bounds(self):
        self.assertTrue(self.hunter._in_window(SLOT, self.target))
        later = replace(SLOT, visit_start='2030-07-20T18:00:00')
        self.assertTrue(self.hunter._in_window(later, self.target))
        bounded = config.normalize_target(dict(TARGET, book_date_to='2030-06-10'), {})
        self.assertTrue(self.hunter._in_window(SLOT, bounded))
        self.assertFalse(self.hunter._in_window(later, bounded))

    def test_unexpected_error_enters_backoff_without_broadcasting_start(self):
        self.hunter.scan_once = Mock(side_effect=UnboundLocalError('test exception'))
        with self.assertLogs('gorzdrav_hunter.hunter', 'INFO'):
            with patch('gorzdrav_hunter.hunter.time.sleep', side_effect=KeyboardInterrupt) as sleep:
                self.assertEqual(130, self.hunter.run(self.state))
        self.assertGreater(sleep.call_args.args[0], self.cfg['poll_interval_sec'])
        self.notifier.broadcast.assert_not_called()

    def test_failed_scan_does_not_report_a_successful_poll(self):
        self.hunter.collect_slots = Mock(side_effect=GorzdravError(-1, 'network error'))
        with self.assertLogs('gorzdrav_hunter.hunter', 'WARNING'):
            with self.assertRaises(GorzdravError):
                self.hunter.scan_once(self.state, persist=False)
        self.assertNotIn('last_scan', self.state)

    def test_slots_reads_current_availability_even_with_autobook_enabled(self):
        self.hunter.collect_slots = Mock(return_value=[SLOT])
        with TemporaryDirectory() as directory:
            cache = Path(directory)
            snapshot = cache / 'manual-slots.json'
            build = (Mock(), self.cfg, self.notifier, self.booker, self.hunter)
            with patch.object(cli, '_build', return_value=build), patch.object(cli, 'CACHE_DIR', cache):
                with patch.object(cli, 'SLOTS_PATH', snapshot), patch('builtins.print'):
                    self.assertEqual(0, cli.cmd_slots(Namespace()))
                    self.assertEqual('slot-1', config._read_json(snapshot)['slots'][0]['id'])
        self.booker.book.assert_not_called()

    def test_confirmed_booking_keeps_existing_history(self):
        self.booker.book.return_value = 'booked'
        self.hunter.collect_slots = Mock(return_value=[SLOT])
        self.cfg['auto_book'] = {'stop_after_booking': True}
        self.state['booked_ids'].append('older-slot')
        self.assertTrue(self.hunter.scan_once(self.state, persist=False))
        self.assertEqual(['older-slot', SLOT.id], self.state['booked_ids'])


if __name__ == '__main__':
    unittest.main()
