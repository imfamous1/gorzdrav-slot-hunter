from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
import subprocess
import sys
import time
import unittest

from gorzdrav_hunter import desktop_worker as worker
from gorzdrav_hunter.booker import Booker
from gorzdrav_hunter.config import _read_json, _write_json
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


if __name__ == '__main__':
    unittest.main()
