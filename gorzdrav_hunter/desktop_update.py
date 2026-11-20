"""Получение и проверка обновлений настольного приложения."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from zipfile import BadZipFile, ZipFile

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import __version__

APP = 'GorzdravSlotHunter'
REPO = 'imfamous1/gorzdrav-slot-hunter'
RELEASES = f'https://github.com/{REPO}/releases'
LATEST = f'https://api.github.com/repos/{REPO}/releases/latest'
MANIFEST = 'update-manifest.json'
SIGNATURE = 'update-manifest.sig'
PUBLIC_KEY = bytes.fromhex('af71db9160383123459e82271f1e0c8ad3140861f3060f6ba60f09d98d6efa8f')
PLATFORMS = ('macOS-arm64', 'macOS-x64', 'Windows-x64')
MAX_ARCHIVE = 512 * 1024 * 1024
WORK_PREFIX = '.gorzdrav-update-'


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class Release:
    tag: str
    asset: str
    size: int
    sha256: str

    @property
    def version(self):
        return self.tag[1:]

    @property
    def url(self):
        return f'{RELEASES}/tag/{self.tag}'


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,6}(\.[0-9]{1,6}){2}', value):
        raise UpdateError('У релиза неверный номер версии.')
    return tuple(map(int, value.split('.')))


def archive_name(platform_name):
    return f'{APP}-{platform_name}.zip'


def desktop_platform():
    machine = platform.machine().lower()
    if sys.platform == 'darwin':
        return {'x86_64': 'macOS-x64', 'arm64': 'macOS-arm64'}.get(machine)
    if sys.platform == 'win32' and machine in ('amd64', 'x86_64'):
        return 'Windows-x64'
    return None


def asset_url(tag, filename):
    return f'{RELEASES}/download/{tag}/{filename}'


def get_bytes(url, limit, *, headers=None):
    with requests.get(url, headers=headers, stream=True, timeout=(5, 15)) as response:
        response.raise_for_status()
        parts = bytearray()
        for chunk in response.iter_content(16384):
            parts.extend(chunk)
            if len(parts) > limit:
                raise UpdateError('GitHub вернул слишком большой ответ.')
        return bytes(parts)


def verify_manifest(content, signature, tag, platform_name):
    try:
        Ed25519PublicKey.from_public_bytes(PUBLIC_KEY).verify(signature, content)
    except InvalidSignature:
        raise UpdateError('Не удалось подтвердить подпись обновления.') from None
    name = archive_name(platform_name)
    try:
        manifest = json.loads(content)
        if manifest['format'] != 1 or manifest['repository'] != REPO or manifest['tag'] != tag:
            raise ValueError
        asset = manifest['assets'][name]
        size, digest = asset['size'], asset['sha256']
        if type(size) is not int or not 0 < size <= MAX_ARCHIVE or not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise UpdateError('Подписанное описание обновления повреждено.') from None
    return Release(tag, name, size, digest)


def check_release():
    platform_name = desktop_platform()
    if not platform_name:
        raise UpdateError('Для этой платформы нет сборок с автообновлением.')
    try:
        latest = json.loads(get_bytes(LATEST, 512 * 1024, headers={'Accept': 'application/vnd.github+json'}))
        tag = latest['tag_name']
        if not isinstance(tag, str) or not tag.startswith('v'):
            raise UpdateError('У релиза неверный номер версии.')
        if latest.get('draft') or latest.get('prerelease') or version_tuple(tag[1:]) <= version_tuple(__version__):
            return None
        published = {asset['name'] for asset in latest['assets'] if asset['state'] == 'uploaded'}
        if not {archive_name(platform_name), MANIFEST, SIGNATURE} <= published:
            raise UpdateError('Подписанная сборка для этого компьютера ещё недоступна.')
        content = get_bytes(asset_url(tag, MANIFEST), 16 * 1024)
        signature = get_bytes(asset_url(tag, SIGNATURE), 64)
    except (requests.RequestException, KeyError, TypeError, ValueError):
        raise UpdateError('Не удалось проверить обновления на GitHub.') from None
    return verify_manifest(content, signature, tag, platform_name)


def download_archive(release, destination):
    digest = hashlib.sha256()
    size = 0
    try:
        with requests.get(asset_url(release.tag, release.asset), stream=True, timeout=(5, 25)) as response:
            response.raise_for_status()
            with destination.open('xb') as stream:
                for chunk in response.iter_content(256 * 1024):
                    size += len(chunk)
                    if size > release.size:
                        break
                    stream.write(chunk)
                    digest.update(chunk)
    except requests.RequestException:
        raise UpdateError('Не удалось скачать обновление с GitHub.') from None
    if size != release.size or digest.hexdigest() != release.sha256:
        raise UpdateError('Скачанный архив не прошёл проверку целостности.')


def bundle_binary(bundle):
    return bundle / ('Contents/MacOS/' + APP if sys.platform == 'darwin' else APP + '.exe')


def installed_bundle():
    if not getattr(sys, 'frozen', False) or not desktop_platform():
        raise UpdateError('Для установки обновления запусти собранное приложение.')
    executable = Path(sys.executable).resolve()
    bundle = executable.parents[2] if sys.platform == 'darwin' else executable.parent
    if bundle_binary(bundle) != executable:
        raise UpdateError('Не удалось определить папку приложения.')
    if sys.platform == 'darwin':
        if bundle.suffix != '.app':
            raise UpdateError('Перенеси приложение в «Программы» и попробуй снова.')
    elif not (bundle / '_internal').is_dir() or {item.name for item in bundle.iterdir()} - {APP + '.exe', '_internal'}:
        raise UpdateError('Распакуй приложение в отдельную папку и попробуй снова.')
    return bundle


def unpack_archive(archive, directory):
    try:
        with ZipFile(archive) as package:
            for entry in package.infolist():
                path = PurePosixPath(entry.filename)
                if path.is_absolute() or '..' in path.parts or '\\' in entry.filename or ':' in entry.filename:
                    raise UpdateError('В архиве обновления неверные пути.')
                if sys.platform == 'win32' and stat.S_ISLNK(entry.external_attr >> 16):
                    raise UpdateError('В архиве обновления найдена недопустимая ссылка.')
            if sys.platform != 'darwin':
                package.extractall(directory)
        if sys.platform == 'darwin':
            subprocess.run(['/usr/bin/ditto', '-x', '-k', str(archive), str(directory)],
                           check=True, timeout=90, capture_output=True)
    except (BadZipFile, subprocess.SubprocessError):
        raise UpdateError('Не удалось распаковать обновление.') from None
    candidate = directory / (APP + '.app' if sys.platform == 'darwin' else APP)
    if not bundle_binary(candidate).is_file():
        raise UpdateError('В архиве обновления отсутствует приложение.')
    return candidate


def prepare_update(release):
    bundle = installed_bundle()
    try:
        work = Path(tempfile.mkdtemp(prefix=WORK_PREFIX, dir=bundle.parent))
    except PermissionError:
        raise UpdateError('У приложения нет прав на обновление в этой папке. Можно скачать архив с GitHub.') from None
    try:
        archive = work / release.asset
        download_archive(release, archive)
        candidate = unpack_archive(archive, work / 'download')
        archive.unlink()
        probe = work / 'probe.txt'
        try:
            subprocess.run(
                [str(bundle_binary(candidate)), '--check', str(probe), release.version], check=True, timeout=50,
                env={**os.environ, 'GORZDRAV_HOME': str(work / 'probe-data'),
                     'PYINSTALLER_RESET_ENVIRONMENT': '1', 'QT_QPA_PLATFORM': 'offscreen'},
                capture_output=True,
            )
        except subprocess.SubprocessError:
            raise UpdateError('Скачанная версия не прошла проверку запуска.') from None
        if not probe.is_file() or probe.read_text(encoding='utf-8') != 'ok':
            raise UpdateError('Скачанная версия не прошла проверку запуска.')
        candidate.rename(work / 'candidate')
        shutil.copytree(bundle, work / 'helper' / bundle.name, symlinks=True)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return work, bundle
