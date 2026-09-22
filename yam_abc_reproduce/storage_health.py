"""Read-only recording-storage preflight; no mkdir, write probe or mount operation.

Mount identity is checked in this process's Linux mount namespace. Call before
creating a recording session, and from a background health check, never a control
tick. A successful snapshot cannot guarantee a later write or prevent hot-unplug;
normal recording I/O errors must still stop recording and surface to the operator.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path


class StorageNotReadyError(OSError):
    """Recording is unavailable, without implying a hardware-control fault."""

    def __init__(self, status: dict):
        self.status = status
        super().__init__("; ".join(status["errors"]))


@dataclass(frozen=True)
class _Mount:
    path: Path
    device: str
    source: str
    readonly: bool


def _mounts() -> list[_Mount]:
    # mountinfo escapes whitespace and backslashes with octal sequences.
    def unescape(value: str) -> str:
        return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)

    result = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        before, after = line.split(" - ", 1)
        fields, filesystem = before.split(), after.split()
        result.append(_Mount(
            Path(unescape(fields[4])), fields[2], unescape(filesystem[1]),
            "ro" in fields[5].split(",") or "ro" in filesystem[2].split(","),
        ))
    return result


def _existing_directory(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        if candidate == candidate.parent:
            raise OSError(f"No existing parent directory for {path}")
        candidate = candidate.parent
    if not candidate.is_dir():
        raise OSError(f"Recording path parent is not a directory: {candidate}")
    return candidate


def _filesystem_device(path: Path) -> str:
    identity = path.stat().st_dev
    return f"{os.major(identity)}:{os.minor(identity)}"


def storage_health(
    path: str | Path,
    *,
    required_mount: str | Path | None = None,
    expected_device: str | Path | None = None,
    min_free_bytes: int = 0,
) -> dict:
    """Return JSON-safe readiness without creating or modifying any path.

    ``required_mount`` enforces a separate mounted filesystem, not merely an
    existing directory. ``expected_device`` can be a stable /dev/disk/by-uuid
    link; it must identify the mount's block device. Without a required mount,
    local development remains supported, but no data-disk isolation is claimed.
    """
    status = {
        "ready": False, "code": "checking", "path": str(path),
        "required_mount": str(required_mount) if required_mount else None,
        "expected_device": str(expected_device) if expected_device else None,
        "mount_enforced": bool(required_mount), "mount_source": None,
        "free_bytes": None, "min_free_bytes": min_free_bytes, "errors": [],
    }

    def fail(code: str, message: str) -> dict:
        status.update(code=code, errors=[message])
        return status

    try:
        if isinstance(min_free_bytes, bool) or not isinstance(min_free_bytes, int) or min_free_bytes < 0:
            return fail("invalid_config", "min_free_bytes must be a nonnegative integer")
        if expected_device and not required_mount:
            return fail("invalid_config", "expected_device requires required_mount")
        target = Path(path).resolve()
        status["path"] = str(target)
        if required_mount:
            mount_path = Path(required_mount)
            if not mount_path.is_absolute():
                return fail("invalid_config", "required_mount must be an absolute path")
            mount_path = mount_path.resolve()
            status["required_mount"] = str(mount_path)
            if mount_path == Path("/"):
                return fail("invalid_config", "The system root cannot be the required data mount")
            if not target.is_relative_to(mount_path):
                return fail("outside_mount", f"Recording path {target} is outside required mount {mount_path}")
            mounts = _mounts()
            exact = [mount for mount in mounts if mount.path == mount_path]
            if not exact:
                return fail("not_mounted", f"Required data filesystem is not mounted at {mount_path}")
            mount = exact[-1]
            roots = [item for item in mounts if item.path == Path("/")]
            if roots and roots[-1].device == mount.device:
                return fail("system_disk", f"Required data mount {mount_path} uses the system root filesystem")
            # An unexpected nested mount must not redirect data to another disk.
            covering = [item for item in mounts if target.is_relative_to(item.path)]
            actual = max(enumerate(covering), key=lambda pair: (len(pair[1].path.parts), pair[0]))[1]
            if actual.path != mount_path:
                return fail("unexpected_mount", f"Recording path is on nested mount {actual.path}, not {mount_path}")
            status["mount_source"] = mount.source
            if mount.readonly:
                return fail("readonly", f"Required data filesystem {mount_path} is read-only")
            if expected_device:
                device = os.stat(expected_device)
                if not stat.S_ISBLK(device.st_mode):
                    return fail("wrong_device", f"Expected data device is not a block device: {expected_device}")
                identity = f"{os.major(device.st_rdev)}:{os.minor(device.st_rdev)}"
                if identity != mount.device:
                    return fail("wrong_device", f"Mount {mount_path} is not backed by {expected_device}")
        ancestor = _existing_directory(target)
        if required_mount and (not ancestor.is_relative_to(mount_path) or _filesystem_device(ancestor) != mount.device):
            return fail("mount_changed", "Recording filesystem changed during storage preflight")
        filesystem = os.statvfs(ancestor)
        if filesystem.f_flag & os.ST_RDONLY:
            return fail("readonly", f"Recording filesystem is read-only: {ancestor}")
        if not os.access(ancestor, os.W_OK | os.X_OK):
            return fail("not_writable", f"Recording directory is not writable/searchable: {ancestor}")
        free = filesystem.f_bavail * filesystem.f_frsize
        status["free_bytes"] = free
        if free < min_free_bytes:
            return fail("low_space", f"Recording filesystem has {free} free bytes; requires {min_free_bytes}")
    except (OSError, ValueError, RuntimeError, IndexError) as exc:
        return fail("check_failed", f"Cannot verify recording storage: {exc}")
    status.update(ready=True, code="ready")
    return status


def recording_storage_health(path: str | Path) -> dict:
    """Apply deployment policy from YAM_RECORDING_* environment variables."""
    raw_minimum = os.environ.get("YAM_RECORDING_MIN_FREE_BYTES", "0")
    try:
        minimum = int(raw_minimum)
    except ValueError:
        status = storage_health(path)
        status.update(ready=False, code="invalid_config", errors=[
            "YAM_RECORDING_MIN_FREE_BYTES must be a nonnegative integer",
        ])
        return status
    return storage_health(
        path, required_mount=os.environ.get("YAM_RECORDING_MOUNT") or None,
        expected_device=os.environ.get("YAM_RECORDING_DEVICE") or None,
        min_free_bytes=minimum,
    )


def require_recording_storage(path: str | Path) -> dict:
    status = recording_storage_health(path)
    if not status["ready"]:
        raise StorageNotReadyError(status)
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--mount", default=os.environ.get("YAM_RECORDING_MOUNT"))
    parser.add_argument("--device", default=os.environ.get("YAM_RECORDING_DEVICE"))
    parser.add_argument("--min-free-bytes", type=int, default=os.environ.get("YAM_RECORDING_MIN_FREE_BYTES", "0"))
    args = parser.parse_args(argv)
    status = storage_health(args.path, required_mount=args.mount, expected_device=args.device,
                            min_free_bytes=args.min_free_bytes)
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0 if status["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
