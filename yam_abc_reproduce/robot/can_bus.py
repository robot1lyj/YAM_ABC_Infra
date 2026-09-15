"""Bring the CAN buses up.

Run at go-live (first Start Teleop) on the hardware path and re-exposed as the GUI
"Reset CAN" action. Reuses i2rt's vendored reset_all_can.sh.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "third_party" / "i2rt" / "scripts" / "reset_all_can.sh"
)


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
    """Bring all ``can*`` interfaces up at 1 Mbit/s. Returns ``(ok, output)``."""
    if not _SCRIPT.exists():
        return False, f"CAN setup script not found: {_SCRIPT}"
    try:
        r = subprocess.run(
            ["bash", str(_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return False, str(exc)
    out = (r.stdout or "").strip()
    if r.returncode == 0:
        return True, out
    return False, (r.stderr or out or f"exit code {r.returncode}").strip()


def bring_up_can_buses(channels: list[str], timeout_s: float = 5.0) -> list[str]:
    """Use the official normal CAN bring-up command, without bouncing live buses.

    ``reset_all_can.sh`` remains an explicit recovery action for an unresponsive
    adapter, not a required step of every arm connection.
    """
    errors: list[str] = []
    for channel in dict.fromkeys(channels):
        try:
            result = subprocess.run(
                ["sudo", "-n", "ip", "link", "set", channel, "up", "type", "can", "bitrate", "1000000"],
                capture_output=True,
                text=True,
                timeout=timeout_s,
                stdin=subprocess.DEVNULL,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            errors.append(f"{channel}: {exc}")
            continue
        if result.returncode:
            detail = (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
            errors.append(f"{channel}: {detail}")
    return errors


def stop_can_buses(channels: list[str], timeout_s: float = 10.0) -> list[str]:
    """Put only the station-owned CAN interfaces DOWN; return failures.

    This is used after the robot stack has released its sockets.  It deliberately
    avoids the board's unrelated ``bcan*`` devices and never masks a shutdown error.
    """
    errors: list[str] = []
    for channel in dict.fromkeys(channels):
        try:
            result = subprocess.run(
                ["sudo", "-n", "ip", "link", "set", channel, "down"],
                capture_output=True,
                text=True,
                timeout=timeout_s,
                stdin=subprocess.DEVNULL,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            errors.append(f"{channel}: {exc}")
            continue
        if result.returncode:
            detail = (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
            errors.append(f"{channel}: {detail}")
    return errors
