"""Собрать архив приложения на текущей операционной системе."""
from __future__ import annotations

from argparse import ArgumentParser
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
APP = 'GorzdravSlotHunter'


def build(output: Path) -> Path:
    if sys.platform not in ('darwin', 'win32'):
        raise SystemExit('Сборка настольного архива доступна на macOS и Windows.')
    output.mkdir(parents=True, exist_ok=True)
    mac = sys.platform == 'darwin'
    arch = {'amd64': 'x64', 'x86_64': 'x64', 'arm64': 'arm64'}.get(platform.machine().lower())
    if not arch:
        raise RuntimeError(f'Архитектура не поддерживается: {platform.machine()}')
    archive = output / f'GorzdravSlotHunter-{"macOS" if mac else "Windows"}-{arch}.zip'
    with TemporaryDirectory(prefix='gorzdrav-build-') as temp:
        work = Path(temp)
        dist = work / 'dist'
        assets = ROOT / 'packaging'
        command = [
            sys.executable, '-m', 'PyInstaller', '--name', APP, '--windowed', '--onedir',
            '--noconfirm', '--specpath', str(work), '--distpath', str(dist),
            '--workpath', str(work / 'build'),
            '--add-data', f"{ROOT / 'ca' / 'russian_trusted_root_ca.pem'}:ca",
            '--add-data', f"{ROOT / 'gorzdrav_hunter' / 'qml'}:gorzdrav_hunter/qml",
            '--add-data', f"{ROOT / 'LICENSE'}:.",
            '--icon', str(assets / ('AppIcon.icns' if mac else 'AppIcon.ico')),
        ]
        if mac:
            command += ['--osx-bundle-identifier', 'org.gorzdrav-slot-hunter.desktop']
        subprocess.run([*command, str(ROOT / 'desktop_app.py')], check=True, cwd=ROOT)

        bundle = dist / f'{APP}.app' if mac else dist / APP
        binary = bundle / 'Contents' / 'MacOS' / APP if mac else bundle / f'{APP}.exe'
        probe = work / 'smoke.txt'
        subprocess.run(
            [str(binary), '--check', str(probe)], check=True, timeout=50,
            env={**os.environ, 'GORZDRAV_HOME': str(work / 'smoke-data')},
        )
        if probe.read_text(encoding='utf-8') != 'ok':
            raise RuntimeError('Проверка запуска приложения не прошла.')

        if mac:
            subprocess.run(
                ['/usr/bin/ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', str(bundle), str(archive)],
                check=True,
            )
        else:
            shutil.make_archive(str(archive.with_suffix('')), 'zip', root_dir=dist, base_dir=APP)
    return archive


if __name__ == '__main__':
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    archive = build(parser.parse_args().output.resolve())
    print(f'{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}')
