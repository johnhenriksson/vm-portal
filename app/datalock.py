"""One portal process per data directory.

The running portal holds an exclusive lock on DATA_DIR/.portal.lock. A second portal on
the same volume refuses to start, and the command-line restore refuses to run while the
portal is up (it would swap the database under a running process). Counted per process,
so test clients sharing one process can each hold it.
"""
import fcntl
import os

from .config import get_bootstrap

_fd: int | None = None
_refs = 0


class DataDirInUse(RuntimeError):
    pass


def acquire(purpose: str = "the portal") -> None:
    global _fd, _refs
    if _refs == 0:
        path = get_bootstrap().data_dir / ".portal.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise DataDirInUse(f"{get_bootstrap().data_dir} is in use by another process; stop it before "
                               f"starting {purpose}") from None
        _fd = fd
    _refs += 1


def release() -> None:
    global _fd, _refs
    _refs = max(0, _refs - 1)
    if _refs == 0 and _fd is not None:
        os.close(_fd)
        _fd = None
