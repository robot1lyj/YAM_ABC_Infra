"""Read-only station doctor diagnostics."""

from __future__ import annotations

import builtins

from yam_abc_reproduce import doctor


def _camera_config(tmp_path):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    (config_dir / "cameras.yaml").write_text(
        "cameras:\n"
        "  - {name: top, type: realsense, serial: '260522275397'}\n"
    )


def test_camera_binding_abi_failure_is_not_reported_as_missing(monkeypatch, tmp_path):
    _camera_config(tmp_path)
    real_import = builtins.__import__

    def incompatible_import(name, *args, **kwargs):
        if name == "pyrealsense2":
            raise ImportError("GLIBC_2.38 not found")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", incompatible_import)
    finding = doctor.check_cameras(tmp_path, "configs/cameras.yaml")[0]

    assert finding.status == doctor.FAIL
    assert "GLIBC_2.38" in finding.detail
    assert "compatible" in finding.fix


def test_missing_camera_binding_remains_a_skipped_optional_dependency(monkeypatch, tmp_path):
    _camera_config(tmp_path)
    real_import = builtins.__import__

    def missing_import(name, *args, **kwargs):
        if name == "pyrealsense2":
            raise ModuleNotFoundError("No module named 'pyrealsense2'", name="pyrealsense2")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_import)
    finding = doctor.check_cameras(tmp_path, "configs/cameras.yaml")[0]

    assert finding.status == doctor.SKIP
    assert finding.detail == "pyrealsense2 not installed"


def test_rkmpp_is_not_expected_on_x86(monkeypatch):
    monkeypatch.setattr(doctor.platform, "machine", lambda: "x86_64")
    assert doctor.check_recording_encoder().status == doctor.SKIP


def test_missing_rkmpp_on_arm_reports_software_fallback(monkeypatch):
    monkeypatch.setattr(doctor.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(doctor.Path, "is_file", lambda _self: False)
    finding = doctor.check_recording_encoder()
    assert finding.status == doctor.WARN
    assert "libx264" in finding.detail
