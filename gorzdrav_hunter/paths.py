"""Ресурсы поставки и отдельный каталог данных приложения."""
import os
import sys
from pathlib import Path

RESOURCES = Path(__file__).resolve().parent.parent
def data_path() -> Path:
    return Path(os.environ.get('GORZDRAV_HOME', RESOURCES)).expanduser().resolve()


def desktop_data() -> Path:
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'Gorzdrav Slot Hunter'
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) / 'GorzdravSlotHunter'
    return Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local' / 'share')) / 'gorzdrav-slot-hunter'
