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

    from gorzdrav_hunter.desktop import DesktopApp
    from tkinter import Tk

    root = Tk()
    app = DesktopApp(root, load_catalog=not args)
    if args[:1] == ['--check'] and len(args) == 2:
        from gorzdrav_hunter.tls import build_bundle

        build_bundle()
        root.update_idletasks()
        Path(args[1]).write_text('ok', encoding='utf-8')
        root.destroy()
        return 0
    root.mainloop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
