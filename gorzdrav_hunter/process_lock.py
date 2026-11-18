"""Блокировка процесса по файлу. ОС освобождает её при завершении процесса."""
from contextlib import contextmanager
import errno
import sys

if sys.platform == 'win32':
    import msvcrt
else:
    import fcntl


@contextmanager
def process_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a+b') as handle:
        try:
            if sys.platform == 'win32':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if isinstance(error, BlockingIOError) or error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise BlockingIOError('Каталог данных используется другим процессом.') from error
            raise
        yield


def is_locked(path) -> bool:
    try:
        with process_lock(path):
            return False
    except BlockingIOError:
        return True
