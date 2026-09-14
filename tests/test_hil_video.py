import numpy as np
import pytest

from yam_abc_reproduce.hil import video


def frame():
    return np.zeros((32, 32, 3), dtype=np.uint8)


def test_forced_software_skips_hardware_probe(monkeypatch):
    monkeypatch.setenv(video.BACKEND_ENV, "libx264")
    monkeypatch.setattr(video, "rkmpp_probe", lambda *_: pytest.fail("unexpected probe"))
    assert video.select_backend(frame(), 30) == "libx264"


def test_auto_uses_verified_hardware_and_falls_back(monkeypatch):
    monkeypatch.setenv(video.BACKEND_ENV, "auto")
    monkeypatch.setattr(video, "rkmpp_probe", lambda *_: None)
    assert video.select_backend(frame(), 30) == "h264_rkmpp"
    monkeypatch.setattr(video, "rkmpp_probe", lambda *_: "not available")
    assert video.select_backend(frame(), 30) == "libx264"


def test_forced_hardware_reports_probe_failure(monkeypatch):
    monkeypatch.setenv(video.BACKEND_ENV, "h264_rkmpp")
    monkeypatch.setattr(video, "rkmpp_probe", lambda *_: "permission denied")
    with pytest.raises(RuntimeError, match="permission denied"):
        video.select_backend(frame(), 30)


def test_invalid_backend_is_rejected(monkeypatch):
    monkeypatch.setenv(video.BACKEND_ENV, "surprise")
    with pytest.raises(ValueError, match="must be"):
        video.select_backend(frame(), 30)
