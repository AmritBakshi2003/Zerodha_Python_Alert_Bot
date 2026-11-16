"""Cross-process, cross-thread lock around read-modify-write of a ``.env`` file.

Both ``SettingsStore.save`` and ``write_access_token`` mutate the same ``.env``
file. Without a shared lock a login landing between another save's read and
replace silently drops the other field (classic lost update). This helper gives
them one lock per resolved path: a ``threading.Lock`` for in-process threads
plus an ``msvcrt``/``fcntl`` advisory lock for cross-process writes (the dashboard
and the bot run as separate processes).

Always re-read the file *inside* the lock.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}


def _thread_lock(path: Path) -> threading.Lock:
    key = os.path.normcase(str(path.absolute()))
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


def _lockfile(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def _acquire_os_lock(fd: int, timeout: float = 10.0) -> None:
    if sys.platform == "win32":
        import msvcrt

        deadline = time.monotonic() + timeout
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
    else:  # pragma: no cover - exercised only on POSIX
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)


def _release_os_lock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        with contextlib.suppress(OSError):
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover - exercised only on POSIX
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def env_write_lock(path: Path, *, timeout: float = 10.0) -> Iterator[None]:
    """Exclusive lock around a read-modify-write of ``path``.

    Acquires a process-wide ``threading.Lock`` (keyed by the resolved path) and
    an OS advisory lock on a sibling ``.lock`` file. Both are released on exit,
    including on exception or process death (the OS releases the advisory lock).
    """
    thread_lock = _thread_lock(path)
    thread_lock.acquire()
    lockfile = _lockfile(path)
    try:
        lockfile.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lockfile), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _acquire_os_lock(fd, timeout=timeout)
            try:
                yield
            finally:
                _release_os_lock(fd)
        finally:
            os.close(fd)
    finally:
        thread_lock.release()


__all__ = ["env_write_lock"]
