"""CAN mutations share the SDK's exclusive per-channel ownership boundary."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .ownership import CanOwnershipError, reserve_can_channels


def list_can_interfaces() -> list[str]:
    """Every CAN interface present on this machine, udev-named or raw (``can_left``,
    ``can0``, ...). Feeds the GUI's channel pickers."""
    net = Path("/sys/class/net")
    if not net.is_dir():
        return []
    return sorted(p.name for p in net.iterdir() if p.name.startswith("can"))


def check_can_up(channels: list[str], timeout_s: float = 5.0) -> list[str]:
    """Return the subset of ``channels`` that are NOT present-and-up."""
    down: list[str] = []
    for ch in channels:
        try:
            r = subprocess.run(
                ["ip", "link", "show", ch],
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except (subprocess.SubprocessError, OSError):
            down.append(ch)
            continue
        out = r.stdout or ""
        if r.returncode != 0 or ("state UP" not in out and "state UNKNOWN" not in out):
            down.append(ch)
    return down


def reset_can_buses(timeout_s: float = 30.0) -> tuple[bool, str]:
    """Explicitly reset the enumerated can* buses, only if none has a live owner.

    Reserve all targets before changing any. Never delegate to a shell script that
    could re-enumerate and reset a newly connected, unreserved interface.
    """
    channels = list_can_interfaces()
    if not channels:
        return False, "No CAN interfaces found"
    try:
        with reserve_can_channels(channels, purpose="explicit CAN reset"):
            for channel in channels:
                for state in ("down", "up"):
                    error = _set_can(channel, state, timeout_s)
                    if error:
                        return False, error
    except (CanOwnershipError, OSError, ValueError) as exc:
        return False, str(exc)
    return True, "CAN reset at 1 Mbit/s: " + ", ".join(channels)


def _set_can(channel, state, timeout_s):
    command = ["sudo", "-n", "ip", "link", "set", channel, state]
    if state == "up":
        command.extend(("type", "can", "bitrate", "1000000"))
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout_s,
                                stdin=subprocess.DEVNULL)
    except (subprocess.SubprocessError, OSError) as exc:
        return f"{channel}: {exc}"
    if result.returncode:
        detail = (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
        return f"{channel}: {detail}"
    return None


def bring_up_can_buses(channels: list[str], timeout_s: float = 5.0) -> list[str]:
    """Use the official normal CAN bring-up command, without bouncing live buses.

    Reset remains an explicit recovery action for an unresponsive adapter, not a
    required step of every arm connection. Live SDK owners cannot be changed.
    """
    errors: list[str] = []
    for channel in dict.fromkeys(channels):
        try:
            with reserve_can_channels([channel], purpose="CAN bring-up"):
                error = _set_can(channel, "up", timeout_s)
        except (CanOwnershipError, OSError, ValueError) as exc:
            errors.append(f"{channel}: {exc}")
            continue
        if error:
            errors.append(error)
    return errors


def stop_can_buses(channels: list[str], timeout_s: float = 10.0) -> list[str]:
    """Put only the station-owned CAN interfaces DOWN; return failures.

    This is used after the robot stack has released its sockets.  It deliberately
    avoids the board's unrelated ``bcan*`` devices and never masks a shutdown error.
    """
    errors: list[str] = []
    for channel in dict.fromkeys(channels):
        try:
            with reserve_can_channels([channel], purpose="CAN shutdown"):
                error = _set_can(channel, "down", timeout_s)
        except (CanOwnershipError, OSError, ValueError) as exc:
            errors.append(f"{channel}: {exc}")
            continue
        if error:
            errors.append(error)
    return errors
