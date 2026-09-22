"""Cooperative per-channel ownership for this project's SDK and CAN entrypoints.

Lock files are stable names and are never unlinked. flock, not file contents or
PID existence, decides ownership. External tools that ignore these locks remain
outside this boundary.
"""

import fcntl
import os
import re
from contextlib import ExitStack, contextmanager
from pathlib import Path
from threading import Lock

LOCK_DIRECTORY = Path("/tmp/yam-can-ownership")


class CanOwnershipError(RuntimeError):
    pass


class CanOwnership:
    def __init__(self, channel, *, purpose="YAM SDK"):
        if not isinstance(channel, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", channel):
            raise ValueError(f"invalid CAN interface name: {channel!r}")
        self.channel = channel
        self.fd = None
        try:
            LOCK_DIRECTORY.mkdir(mode=0o1777)
        except FileExistsError:
            pass
        else:
            LOCK_DIRECTORY.chmod(0o1777)
        path = LOCK_DIRECTORY / f"{channel}.lock"
        flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o666)
        except FileExistsError:
            fd = os.open(path, flags)
        else:
            os.fchmod(fd, 0o666)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                owner = os.pread(fd, 256, 0).decode(errors="replace").strip()
                raise CanOwnershipError(
                    f"CAN {channel} is owned by {owner or 'another device process'}; "
                    "close that owner safely before connecting or changing CAN"
                ) from None
            os.ftruncate(fd, 0)
            os.write(fd, f"PID {os.getpid()} ({purpose})\n".encode())
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd

    def close(self):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            os.close(fd)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


@contextmanager
def reserve_can_channels(channels, *, purpose):
    """Acquire every requested channel before performing any CAN mutation."""
    with ExitStack() as stack:
        for channel in sorted(set(channels)):
            stack.enter_context(CanOwnership(channel, purpose=purpose))
        yield


def construct_owned_yam(channel, factory):
    """Retain ownership until a successful SDK close, including uncertain startup.

The caller must resolve dependencies/configuration before entering this function.
The upstream factory may start CAN threads and then fail without returning the
SDK. In that case the raw fd deliberately stays open until process exit: dropping
the lock would advertise an unproven cleanup as safe for another controller.
"""
    ownership = CanOwnership(channel)
    try:
        robot = factory()
    except BaseException as exc:
        raise RuntimeError(
            f"CAN {channel}: SDK startup failed with uncertain cleanup: {exc}. "
            "Ownership retained; support the arms and restart this owner process."
        ) from exc
    original_close = robot.close
    closed = False
    close_lock = Lock()

    def close_owned():
        nonlocal closed
        with close_lock:
            if closed:
                return
            original_close()  # Failure retains the lock; explicit close may be retried.
            ownership.close()
            closed = True

    robot.close = close_owned
    return robot
