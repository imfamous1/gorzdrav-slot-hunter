"""Подписать манифест окончательных ZIP-файлов для GitHub Release."""
from argparse import ArgumentParser
import hashlib
import json
from pathlib import Path
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gorzdrav_hunter import __version__
from gorzdrav_hunter.desktop_update import MANIFEST, MAX_ARCHIVE, PLATFORMS, PUBLIC_KEY, REPO, SIGNATURE, archive_name


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--key', type=Path, required=True, help='Локальный закрытый ключ Ed25519, 32 байта')
    parser.add_argument('archives', type=Path, help='Папка с тремя финальными архивами релиза')
    args = parser.parse_args()

    key = Ed25519PrivateKey.from_private_bytes(args.key.read_bytes())
    if key.public_key().public_bytes_raw() != PUBLIC_KEY:
        parser.error('Закрытый ключ не соответствует ключу, встроенному в приложение.')

    assets = {}
    for platform_name in PLATFORMS:
        archive = args.archives / archive_name(platform_name)
        size = archive.stat().st_size
        if not 0 < size <= MAX_ARCHIVE:
            parser.error(f'Размер архива вне диапазона: {archive.name}')
        with archive.open('rb') as file:
            assets[archive.name] = {'size': size, 'sha256': hashlib.file_digest(file, 'sha256').hexdigest()}

    manifest = {'format': 1, 'repository': REPO, 'tag': f'v{__version__}', 'assets': assets}
    content = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8')
    (args.archives / MANIFEST).write_bytes(content)
    (args.archives / SIGNATURE).write_bytes(key.sign(content))
    print(f"Манифест и подпись для {manifest['tag']} готовы.")


if __name__ == '__main__':
    main()
