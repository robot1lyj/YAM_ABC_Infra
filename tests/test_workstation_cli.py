"""CLI checks must remain free of device and recording side effects."""
import json
import sys

import pytest

from yam_abc_reproduce.hil import run


def forbid(*args, **kwargs):
    pytest.fail("preflight constructed a runtime resource")


def invoke(monkeypatch, tmp_path, *args):
    for name in ("build_arm_units", "build_cameras_from_config", "RecordingSession", "PolicyWorker"):
        monkeypatch.setattr(run, name, forbid)
    monkeypatch.setattr(sys, "argv", ["yam-workstation", "--check", "--output", str(tmp_path / "session"), *args])
    run.main()
    assert not (tmp_path / "session").exists()


@pytest.mark.parametrize("mode", ["teleop", "inference", "hil", "collect"])
def test_mock_preflight(monkeypatch, tmp_path, capsys, mode):
    invoke(monkeypatch, tmp_path, "--mock", "--mode", mode)
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == mode
    assert report["hardware_checked"] is False


def test_real_preflight_uses_verified_config_without_devices(monkeypatch, tmp_path, capsys):
    invoke(monkeypatch, tmp_path, "--mode", "collect")
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "collect"
    assert report["hardware_checked"] is False


def test_missing_optional_dependency(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(run, "find_spec", lambda name: None if name == "fastapi" else object())
    with pytest.raises(SystemExit) as exc:
        invoke(monkeypatch, tmp_path, "--mock", "--web-port", "8766")
    assert exc.value.code == 2
    assert "uv sync --locked" in capsys.readouterr().err


@pytest.mark.parametrize("port", ["0", "-1", "65536"])
def test_invalid_port(monkeypatch, tmp_path, port):
    with pytest.raises(SystemExit) as exc:
        invoke(monkeypatch, tmp_path, "--mock", "--web-port", port)
    assert exc.value.code == 2
