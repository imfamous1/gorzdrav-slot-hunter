"""TLS-верификация для gorzdrav.spb.ru.

Сертификаты портала выпущены «Russian Trusted Sub CA» (НУЦ Минцифры), этого
корня нет в хранилище certifi/Mozilla. Собираем бандл: общедоверенные CA
(certifi или /etc/ssl/cert.pem) + корень НУЦ из ca/ проекта. Корень скачан с
официального источника gu-st.ru; sha256 сверяется при каждом запуске.
"""
from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from pathlib import Path
from .paths import RESOURCES, data_path

log = logging.getLogger(__name__)

RU_ROOT_CA = RESOURCES / 'ca' / 'russian_trusted_root_ca.pem'
# SHA-256 PEM-файла из официального источника.
EXPECTED_SHA256 = '936a43fea6e8e525bcc0f81acd9c3d21b4fc4b9b68acea7906d698005afc6504'

CACHE_DIR = data_path() / '.cache'
BUNDLE_PATH = CACHE_DIR / 'ca-bundle.pem'


def _base_bundle() -> Path | None:
    """Лучший из доступных общедоверенных бандлов."""
    try:
        import certifi
        return Path(certifi.where())
    except ImportError:
        pass
    system = Path('/etc/ssl/cert.pem')
    return system if system.exists() else None


def build_bundle() -> str:
    """Собрать PEM-бандл (базовые CA + корень НУЦ) и вернуть его путь."""
    if not RU_ROOT_CA.exists():
        raise FileNotFoundError(
            f'Нет файла {RU_ROOT_CA}. Верни корневой сертификат НУЦ Минцифры '
            '(https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt) '
            'в ca/russian_trusted_root_ca.pem'
        )
    digest = hashlib.sha256(RU_ROOT_CA.read_bytes()).hexdigest()
    if digest != EXPECTED_SHA256:
        raise ValueError('SHA-256 корневого сертификата не совпал. '
                         'Проверь файл в ca/ и установи актуальную версию проекта.')
    base = _base_bundle()
    if base is None:
        raise RuntimeError(
            'Не найден базовый CA-бандл: поставь certifi (pip install certifi)'
        )
    BUNDLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    combined = base.read_text(encoding='utf-8').rstrip() + '\n\n' + \
        RU_ROOT_CA.read_text(encoding='utf-8').rstrip() + '\n'
    handle = tempfile.NamedTemporaryFile(
        mode='w', dir=CACHE_DIR, encoding='utf-8', delete=False,
    )
    tmp = Path(handle.name)
    try:
        with handle:
            handle.write(combined)
        os.replace(tmp, BUNDLE_PATH)
    finally:
        tmp.unlink(missing_ok=True)
    return str(BUNDLE_PATH)


def resolve_verify(insecure: bool = False) -> str | bool:
    """Параметр verify для requests: путь к бандлу или False при --insecure."""
    if insecure:
        log.warning(
            'TLS-верификация ОТКЛЮЧЕНА (--insecure / insecure_tls). '
            'Не оставляй так на постоянку.'
        )
        return False
    return build_bundle()
