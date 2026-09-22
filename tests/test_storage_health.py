import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from yam_abc_reproduce import storage_health as storage


@pytest.fixture
def data_mount(tmp_path, monkeypatch):
    mount = tmp_path / "data"
    mount.mkdir()
    mounts = [
        storage._Mount(Path("/"), "8:0", "/dev/system", False),
        storage._Mount(mount, "259:1", "/dev/nvme0n1p1", False),
    ]
    monkeypatch.setattr(storage, "_mounts", lambda: mounts)
    monkeypatch.setattr(storage, "_filesystem_device", lambda path: "259:1")
    monkeypatch.setattr(storage.os, "statvfs", lambda path: SimpleNamespace(
        f_flag=0, f_bavail=1024, f_frsize=4096,
    ))
    for name in ("YAM_RECORDING_MOUNT", "YAM_RECORDING_DEVICE", "YAM_RECORDING_MIN_FREE_BYTES"):
        monkeypatch.delenv(name, raising=False)
    return mount, mounts


def test_ready_check_does_not_create_output(data_mount):
    mount, _ = data_mount
    output = mount / "new_task" / "new_session"
    status = storage.storage_health(output, required_mount=mount, min_free_bytes=1)
    assert status["ready"] and status["mount_enforced"]
    assert status["free_bytes"] == 4194304
    assert not output.parent.exists()


def test_existing_directory_is_not_a_mount(data_mount, monkeypatch):
    mount, mounts = data_mount
    monkeypatch.setattr(storage, "_mounts", lambda: mounts[:1])
    monkeypatch.setenv("YAM_RECORDING_MOUNT", str(mount))
    output = mount / "episodes"
    assert mount.is_dir()
    with pytest.raises(storage.StorageNotReadyError) as caught:
        storage.require_recording_storage(output)
    assert caught.value.status["code"] == "not_mounted"
    assert not output.exists()


@pytest.mark.parametrize("class_name", ["Recorder", "RecordingSession"])
def test_recording_constructor_refuses_missing_mount_before_mkdir(data_mount, monkeypatch, class_name):
    from yam_abc_reproduce.hil import recording

    mount, mounts = data_mount
    monkeypatch.setattr(storage, "_mounts", lambda: mounts[:1])
    monkeypatch.setenv("YAM_RECORDING_MOUNT", str(mount))
    output = mount / "session"
    with pytest.raises(storage.StorageNotReadyError, match="not mounted"):
        getattr(recording, class_name)(output)
    assert not output.exists()


def test_task_binding_refuses_disappeared_mount(data_mount, monkeypatch):
    from yam_abc_reproduce.hil.recording import RecordingSession

    mount, mounts = data_mount
    monkeypatch.setenv("YAM_RECORDING_MOUNT", str(mount))
    session = RecordingSession(mount / "unbound")
    try:
        monkeypatch.setattr(storage, "_mounts", lambda: mounts[:1])
        with pytest.raises(storage.StorageNotReadyError):
            session.bind_task(mount / "new-task" / "session", {})
        assert session.path == mount / "unbound"
        assert not (mount / "new-task").exists()
    finally:
        session.close("aborted")


def test_mount_disappears_during_check(data_mount, monkeypatch):
    mount, _ = data_mount
    monkeypatch.setattr(storage, "_filesystem_device", lambda path: "8:0")
    assert storage.storage_health(mount, required_mount=mount)["code"] == "mount_changed"


def test_symlink_escape_and_nested_mount_fail(data_mount, tmp_path):
    mount, mounts = data_mount
    (mount / "escape").symlink_to(tmp_path)
    assert storage.storage_health(mount / "escape" / "recording", required_mount=mount)["code"] == "outside_mount"
    nested = mount / "nested"
    nested.mkdir()
    mounts.append(storage._Mount(nested, "8:2", "/dev/other", False))
    assert storage.storage_health(nested / "recording", required_mount=mount)["code"] == "unexpected_mount"


@pytest.mark.parametrize("device,readonly,code", [("8:0", False, "system_disk"), ("259:1", True, "readonly")])
def test_root_bind_and_readonly_mount_fail(data_mount, device, readonly, code):
    mount, mounts = data_mount
    mounts[-1] = storage._Mount(mount, device, "/dev/test", readonly)
    assert storage.storage_health(mount, required_mount=mount)["code"] == code


def test_device_identity_uses_block_device_number(data_mount, monkeypatch):
    mount, _ = data_mount
    original_stat = os.stat
    identity = SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(259, 1))

    def fake_stat(path, *args, **kwargs):
        return identity if str(path) == "/dev/disk/by-uuid/test" else original_stat(path, *args, **kwargs)

    monkeypatch.setattr(storage.os, "stat", fake_stat)
    options = dict(required_mount=mount, expected_device="/dev/disk/by-uuid/test")
    assert storage.storage_health(mount, **options)["ready"]
    identity.st_rdev = os.makedev(259, 2)
    assert storage.storage_health(mount, **options)["code"] == "wrong_device"
    identity.st_mode = stat.S_IFREG
    assert storage.storage_health(mount, **options)["code"] == "wrong_device"


def test_readonly_flag_permissions_and_free_space(data_mount, monkeypatch):
    mount, _ = data_mount
    assert storage.storage_health(mount, required_mount=mount, min_free_bytes=4194305)["code"] == "low_space"
    monkeypatch.setattr(storage.os, "access", lambda *args: False)
    assert storage.storage_health(mount, required_mount=mount)["code"] == "not_writable"
    monkeypatch.setattr(storage.os, "statvfs", lambda path: SimpleNamespace(f_flag=os.ST_RDONLY))
    assert storage.storage_health(mount, required_mount=mount)["code"] == "readonly"


@pytest.mark.parametrize("value", ["nan", "-1"])
def test_invalid_environment_fails_closed(data_mount, monkeypatch, value):
    mount, _ = data_mount
    monkeypatch.setenv("YAM_RECORDING_MIN_FREE_BYTES", value)
    with pytest.raises(storage.StorageNotReadyError) as caught:
        storage.require_recording_storage(mount)
    assert caught.value.status["code"] == "invalid_config"


def test_device_without_mount_and_root_mount_are_configuration_errors(data_mount):
    mount, _ = data_mount
    assert storage.storage_health(mount, expected_device="/dev/test")["code"] == "invalid_config"
    assert storage.storage_health(mount, required_mount="/")["code"] == "invalid_config"


def test_mountinfo_unavailable_fails_closed(data_mount, monkeypatch):
    mount, _ = data_mount

    def inaccessible():
        raise OSError("mountinfo unavailable")

    monkeypatch.setattr(storage, "_mounts", inaccessible)
    assert storage.storage_health(mount, required_mount=mount)["code"] == "check_failed"


def test_mountinfo_escaping(monkeypatch):
    monkeypatch.setattr(Path, "read_text", lambda *args: (
        "12 1 259:1 / /data\\040disk rw,nosuid shared:9 - ext4 /dev/disk\\134name rw\n"
    ))
    assert storage._mounts() == [storage._Mount(Path("/data disk"), "259:1", "/dev/disk\\name", False)]


def test_cli_is_stdlib_only_and_reports_missing_mount(tmp_path):
    # -S removes third-party site-packages; no imports of robot/camera/data stacks.
    output = tmp_path / "not_created"
    result = subprocess.run(
        [sys.executable, "-S", "-m", "yam_abc_reproduce.storage_health", str(output), "--mount", str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        env={**os.environ, "YAM_RECORDING_DEVICE": "", "YAM_RECORDING_MIN_FREE_BYTES": "0"}, check=False,
    )
    assert result.returncode == 2
    assert '"code": "not_mounted"' in result.stdout
    assert not output.exists()


def test_system_disk_templates_keep_web_independent_and_data_absolute():
    deploy = Path(__file__).resolve().parents[1] / "deploy"
    web = (deploy / "yam-workstation.service").read_text()
    assert "Requires=yam-device.service" not in web
    assert "Wants=network-online.target yam-device.service" in web
    assert "WorkingDirectory=/data/YAM" in web  # Existing install stays compatible.
    for role in ("yam-workstation", "yam-device", "yam-executor"):
        override = (deploy / "system-disk" / f"{role}.conf.example").read_text()
        assert "WorkingDirectory=%h/YAM-runtime" in override
        assert "ExecStart=\nExecStart=%h/YAM-runtime/.venv/bin/" in override
        assert "RequiresMountsFor=" not in override
    device = (deploy / "system-disk" / "yam-device.conf.example").read_text()
    assert "--output /data/YAM/data/episodes" in device
    assert "Environment=YAM_RECORDING_MOUNT=/data" in device
    assert "Environment=YAM_RECORDING_DEVICE=/dev/disk/by-uuid/" in device
