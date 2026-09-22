"""API admission stays responsive while data-only recovery waits on storage."""

import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from yam_abc_reproduce.hil import workbench
from yam_abc_reproduce.hil.web import create_app


def test_recovery_returns_before_disk_and_failed_recovery_is_retryable(monkeypatch, tmp_path):
    service = workbench.Workbench.__new__(workbench.Workbench)
    service.args = SimpleNamespace(mock=True)
    service._lock = threading.Lock()
    service.state = "connected"
    service.initializing = False
    service._recording_recovery_thread = None
    service.runtime = SimpleNamespace(
        status={"phase": "hold"}, recording_error="disk error",
        recorder=SimpleNamespace(error="disk error"),
    )
    service.recording_error = "disk error"
    service.log = lambda message: None
    entered, finish = threading.Event(), threading.Event()
    attempts = []

    @contextmanager
    def barrier(runtime, *, recovering):
        assert recovering and runtime is service.runtime
        yield SimpleNamespace(committed=False)

    def recover(runtime, change):
        attempts.append(True)
        if len(attempts) == 1:
            entered.set()
            assert finish.wait(3)
            raise OSError("storage still missing")
        return tmp_path / "new-session"

    monkeypatch.setattr(workbench, "paused_data_session", barrier)
    monkeypatch.setattr(workbench, "recover_recording", recover)
    events = []
    api = SimpleNamespace(status={"phase": "hold"}, event=events.append,
                          restart_recording=service.restart_recording)
    headers = {"X-YAM-Control": "1"}
    try:
        with TestClient(create_app(api)) as client:
            assert client.post("/recording/restart", headers=headers).status_code == 200
            assert entered.wait(1) and not finish.is_set()
            assert service.recording_recovery["state"] == "recovering"
            assert client.get("/status").status_code == 200
            assert client.post("/event/hold", headers=headers).status_code == 200
            assert client.post("/recording/restart", headers=headers).status_code == 409
            with pytest.raises(ValueError, match="本次未提交"):
                service.disconnect(supported=True)
            finish.set()
            service._recording_recovery_thread.join(2)
            assert service.recording_recovery == {"state": "failed", "error": "storage still missing"}
            assert service.recording_error == "disk error"
            assert client.post("/recording/restart", headers=headers).status_code == 200
            service._recording_recovery_thread.join(2)
            assert service.recording_recovery == {"state": "complete", "error": None}
            assert service.output == tmp_path / "new-session"
            assert events == ["hold"]  # No automatic connect/start/disconnect.
    finally:
        finish.set()
        if service._recording_recovery_thread:
            service._recording_recovery_thread.join(3)


def test_recovery_thread_start_failure_allows_explicit_retry(monkeypatch):
    service = workbench.Workbench.__new__(workbench.Workbench)
    service._lock = threading.Lock()
    service.state, service.initializing = "connected", False
    service._recording_recovery_thread = None
    service.runtime = SimpleNamespace(status={"phase": "hold"}, recording_error="disk error")
    monkeypatch.setattr(threading.Thread, "start", lambda thread: (_ for _ in ()).throw(RuntimeError("start failed")))
    for _ in range(2):
        with pytest.raises(RuntimeError, match="start failed"):
            service.restart_recording()
        assert service.recording_recovery == {"state": "failed", "error": "start failed"}
