from __future__ import annotations

import fcntl
import os
from pathlib import Path


class RunLock:
    """Exclusive OS-level lock: only one run at a time across the server and the CLI."""

    def __init__(self, path: Path):
        self._path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def is_held_elsewhere(self) -> bool:
        if self._fd is not None:
            return False
        if self.acquire():
            self.release()
            return False
        return True
