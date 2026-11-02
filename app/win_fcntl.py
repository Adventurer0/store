"""Windows compatibility shim for the Unix-only ``fcntl.flock``.

ShopDock is built for Linux containers. This module lets the same source run on
Windows for a local preview by mapping ``flock`` onto ``msvcrt.locking``. Only
the subset used by ``app/jobs.py`` (LOCK_EX / LOCK_NB / LOCK_UN on a file
handle) is provided.
"""
import msvcrt

LOCK_SH = 1
LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8


def flock(file, operation):
    """Advisory lock on ``file`` (a file object). Raises BlockingIOError when busy."""
    fd = file.fileno()
    try:
        file.seek(0)
    except (OSError, ValueError):
        pass
    if operation & LOCK_UN:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    mode = msvcrt.LK_NBLCK if operation & LOCK_NB else msvcrt.LK_LOCK
    try:
        msvcrt.locking(fd, mode, 1)
    except OSError as exc:
        if operation & LOCK_NB:
            raise BlockingIOError(exc.errno, str(exc)) from None
        raise
