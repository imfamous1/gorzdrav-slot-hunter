import hashlib
from importlib.util import find_spec
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from unittest.mock import Mock, patch


@skipUnless(find_spec('cryptography'), 'desktop update dependencies are not installed')
class DesktopUpdateTests(TestCase):
    def setUp(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        from gorzdrav_hunter import desktop_update, desktop_install

        self.update = desktop_update
        self.install = desktop_install
        self.key = Ed25519PrivateKey.generate()
        self.archive = b'fixture archive'
        self.tag = 'v9.2.0'
        self.platform = 'macOS-arm64'
        self.asset = desktop_update.archive_name(self.platform)
        self.digest = hashlib.sha256(self.archive).hexdigest()
        self.manifest = json.dumps({
            'format': 1, 'repository': desktop_update.REPO, 'tag': self.tag,
            'assets': {self.asset: {'size': len(self.archive), 'sha256': self.digest}},
        }).encode()

    def check_release(self, content, signature):
        update = self.update
        metadata = json.dumps({'tag_name': self.tag, 'assets': [
            {'name': name, 'state': 'uploaded'} for name in (self.asset, update.MANIFEST, update.SIGNATURE)
        ]}).encode()
        with patch.multiple(update, PUBLIC_KEY=self.key.public_key().public_bytes_raw(),
                            desktop_platform=Mock(return_value=self.platform),
                            get_bytes=Mock(side_effect=[metadata, content, signature])):
            return update.check_release()

    def test_github_update_requires_a_signed_manifest(self):
        signature = self.key.sign(self.manifest)
        release = self.check_release(self.manifest, signature)
        self.assertEqual((self.tag, self.asset, len(self.archive), self.digest),
                         (release.tag, release.asset, release.size, release.sha256))
        with self.assertRaises(self.update.UpdateError):
            self.check_release(self.manifest + b' ', signature)
        with self.assertRaises(self.update.UpdateError):
            other_version = self.manifest.replace(b'v9.2.0', b'v9.2.1')
            self.check_release(other_version, self.key.sign(other_version))

    def test_download_rejects_modified_archive(self):
        release = self.check_release(self.manifest, self.key.sign(self.manifest))
        with TemporaryDirectory() as folder, patch.object(self.update.requests, 'get') as request:
            response = request.return_value.__enter__.return_value
            response.iter_content.return_value = [self.archive]
            path = Path(folder) / self.asset
            self.update.download_archive(release, path)
            self.assertEqual(self.archive, path.read_bytes())
            path.unlink()
            response.iter_content.return_value = [self.archive.replace(b'archive', b'changed')]
            with self.assertRaises(self.update.UpdateError):
                self.update.download_archive(release, path)

    def test_failed_swap_restores_previous_application(self):
        with TemporaryDirectory() as folder:
            parent = Path(folder)
            work = parent / 'work'
            target = parent / 'installed'
            candidate = work / 'candidate'
            candidate.mkdir(parents=True)
            target.mkdir()
            (target / 'version').write_text('previous')

            def move(source, destination):
                if source == candidate:
                    raise PermissionError
                source.rename(destination)

            with patch.object(self.install, '_move_bundle', side_effect=move), self.assertRaises(PermissionError):
                self.install.replace_bundle(work, target)
            self.assertEqual('previous', (target / 'version').read_text())

    def test_resume_reuses_search_without_rewriting_plan_or_transfer_state(self):
        from contextlib import nullcontext

        from gorzdrav_hunter.config import _read_json, _write_json

        install = self.install
        job = {'run': {'worker': 'active', 'token': 'same'}, 'revision': 'same'}
        config = {'desktop_plan': {'patients': {}, 'jobs': {'search': job}}}
        transfer = {'run_token': 'same', 'revision': 'same', 'phase': 'watching',
                    'reschedules': {'fixture': {'status': 'submitted'}}}
        state = {'desktop_jobs': {'search': transfer}, 'last_scan': '2026-11-20T12:00:00'}
        with TemporaryDirectory() as folder:
            stop = Path(folder) / 'stop.json'
            config_path = Path(folder) / 'config.json'
            _write_json(stop, {'id': 'active'})
            _write_json(config_path, config)
            with patch.multiple(
                install, STOP=stop, CONFIG_PATH=config_path, _control_update=nullcontext,
                read_status=Mock(return_value={'id': 'active', 'phase': 'stopped'}),
                load_config=Mock(return_value={'poll_interval_sec': 60}), load_state=Mock(return_value=state),
                _launch_worker=Mock(),
            ):
                self.assertTrue(install._resume_search(install.PausedSearch('active', {'id': 'older'}), Path('app')))
                self.assertEqual(('active',), install._launch_worker.call_args.args)
                self.assertTrue(install._launch_worker.call_args.kwargs['per_job'])
            self.assertEqual({'id': 'older'}, _read_json(stop))
            self.assertEqual(config, _read_json(config_path))
            self.assertEqual({'status': 'submitted'}, transfer['reschedules']['fixture'])
