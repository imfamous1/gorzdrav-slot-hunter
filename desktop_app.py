"""Точка входа настольной версии и её фонового процесса."""
from pathlib import Path
import os
import sys


def main():
    # Выбираем каталог до импорта модуля с путями к конфигурации CLI.
    from gorzdrav_hunter.paths import desktop_data

    os.environ.setdefault('GORZDRAV_HOME', str(desktop_data()))
    os.umask(0o077)
    args = sys.argv[1:]
    if args[:1] == ['--worker'] and len(args) == 2:
        from gorzdrav_hunter.desktop_worker import run_worker
        return run_worker(args[1])
    if args[:1] == ['--install-update'] and len(args) == 2:
        from gorzdrav_hunter.desktop_install import run_installer
        return run_installer(Path(args[1]))

    from gorzdrav_hunter.desktop import create_window

    checking = args[:1] == ['--check'] and len(args) in (2, 3)
    updated = args[:1] == ['--update-finished'] and len(args) == 2
    if checking and len(args) == 3:
        from gorzdrav_hunter import __version__
        if args[2] != __version__:
            return 1
    application, engine, controller = create_window(load_catalog=not checking, check_updates=not args)
    if checking:
        from gorzdrav_hunter.tls import build_bundle

        build_bundle()
        application.processEvents()
        Path(args[1]).write_text('ok', encoding='utf-8')
        engine.rootObjects()[0].close()
        return 0
    if updated:
        controller.updateFinished(Path(args[1]).resolve())
    try:
        return application.exec()
    finally:
        controller.releaseUpdateLock()


if __name__ == '__main__':
    raise SystemExit(main())
