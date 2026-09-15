"""Optional per-role CPU placement for the RK3588 workstation."""

from __future__ import annotations

import os


def place_on_cpus(role: str) -> tuple[int, ...] | None:
    """Pin only when a deployment explicitly configures this role.

    Linux affinity is per thread. Spawned worker threads and FFmpeg subprocesses
    inherit the caller's mask unless they set their own placement.
    """
    name = f"YAM_ABC_{role.upper()}_CPUS"
    setting = os.environ.get(name)
    if setting is None:
        return None
    try:
        fields = [part.strip() for part in setting.split(",")]
        if not fields or any(not field.isdecimal() for field in fields):
            raise ValueError("expected comma-separated CPU numbers")
        cpus = {int(field) for field in fields}
        count = os.cpu_count()
        if count is not None and any(cpu >= count for cpu in cpus):
            raise ValueError(f"CPU number must be below {count}")
    except ValueError as exc:
        raise ValueError(f"invalid {name}={setting!r}: {exc}") from exc
    if not hasattr(os, "sched_setaffinity"):
        raise RuntimeError(f"{name} requires Linux CPU affinity")
    try:
        os.sched_setaffinity(0, cpus)
    except OSError as exc:
        raise RuntimeError(f"cannot apply {name}={setting!r}: {exc}") from exc
    return tuple(sorted(os.sched_getaffinity(0)))
