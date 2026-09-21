"""Fault injection without any robot, CAN adapter or model server."""

import queue
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from yam_abc_reproduce.hil.policy import PolicyWorker
from yam_abc_reproduce.hil.web import create_app
from yam_abc_reproduce.hil.workbench import Workbench


def test_transport_failure_survives_planner_success():
    done = threading.Event()
    def fail():
        raise RuntimeError("transport failed")
    client = SimpleNamespace(restart=fail, close=lambda: None)
    planner = SimpleNamespace(restart=done.set, close=lambda: None, alive=True)
    worker = PolicyWorker(client, planner=planner)
    try:
        assert done.wait(1)
        assert not worker.ready
        assert worker.restart_error == "transport failed"
    finally:
        worker.close()


def test_only_operator_heartbeat_and_stop_from_other_tab(monkeypatch):
    from yam_abc_reproduce.hil import web
    clock = [100.0]
    monkeypatch.setattr(web, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    events, beats = [], []
    owner = SimpleNamespace(status={}, event=events.append, heartbeat=lambda: beats.append(1))
    app = create_app(owner, control_access=True)
    base = {"X-YAM-Control": "1"}
    a, b = {**base, "X-YAM-Session": "a"}, {**base, "X-YAM-Session": "b"}
    with TestClient(app) as client:
        assert client.post("/event/start", headers={"X-YAM-Control": "1"}).status_code == 409
        assert client.post("/event/start", headers=a).status_code == 200
        assert client.post("/heartbeat", headers=b).json()["operator"] is False
        assert not beats
        assert client.post("/heartbeat", headers=a).status_code == 200
        assert beats == [1]
        assert client.post("/event/start", headers=b).status_code == 409
        assert client.post("/event/stop", headers=b).status_code == 200
        assert client.get("/status").status_code == 200
        assert events == ["start", "stop"]
        clock[0] += 4
        assert client.post("/heartbeat", headers=a).json() == {"ok": True}
        assert events == ["start", "stop"]  # Recovery never requests movement.
        clock[0] += 4
        assert client.post("/event/start", headers=b).status_code == 200
        assert client.post("/heartbeat", headers=a).json()["operator"] is False
        assert beats == [1, 1]


def test_visible_page_attaches_after_web_restart_without_motion():
    beats, events = [], []
    owner = SimpleNamespace(status={}, event=events.append, heartbeat=lambda: beats.append(1))
    with TestClient(create_app(owner, control_access=True)) as client:
        headers = {"X-YAM-Control": "1", "X-YAM-Session": "refreshed", "X-YAM-Visible": "1"}
        assert client.post("/heartbeat", headers=headers).json() == {"ok": True}
        other = {**headers, "X-YAM-Session": "other"}
        assert client.post("/heartbeat", headers=other).json()["operator"] is False
        assert beats == [1] and events == []


def test_recorder_hung_request_is_bounded():
    from yam_abc_reproduce.hil.recording_service import RemoteRecordingSession
    service = RemoteRecordingSession.__new__(RemoteRecordingSession)
    service.command_timeout_s = 0.01
    service._thread = SimpleNamespace(is_alive=lambda: True)
    service.save_progress_at = lambda: 0
    service._enqueue = lambda *a, **kw: True
    service._error = None
    start = time.monotonic()
    with pytest.raises(RuntimeError, match="outcome uncertain"):
        service._request("rotate", "unused", {})
    assert time.monotonic() - start < 1


def test_catalog_rollback_on_session_failure(tmp_path):
    service = Workbench(SimpleNamespace(mode="collect", mock=True, url=None, task_root=tmp_path))
    try:
        original = service.create_task("old", "old", "pick lego")
        def fail(_):
            raise ValueError("rotation failed")
        service._bind_task = fail
        with pytest.raises(ValueError, match="rotation failed"):
            service.update_task(original["id"], "new", "new", "sort lego")
        assert service.tasks.get(original["id"])["name"] == "old"
        assert service.selected_task == original
        with pytest.raises(ValueError):
            service.create_task("another", "another", "pick lego")
        assert len(service.tasks.items) == 1
    finally:
        service.close()


def test_health_sampler_recovers_after_exception():
    service = Workbench.__new__(Workbench)
    service._closing = threading.Event()
    service._health_error = None
    messages, calls = [], []
    service.log = messages.append
    def sample():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("camera temporarily unavailable")
        service._closing.set()
    service._observe_loop = sample
    service._observe()
    assert len(calls) == 2 and len(messages) == 1


def test_policy_command_refused_during_switch():
    from yam_abc_reproduce.hil.run import Runtime
    runtime = Runtime.__new__(Runtime)
    runtime.task_switching = True
    runtime.policy_commands = queue.Queue()
    with pytest.raises(ValueError, match="设置未提交"):
        runtime._queue_policy_command(("restart",))
    assert runtime.policy_commands.empty()


def test_runtime_error_does_not_break_status_or_stop():
    def fail(**kwargs):
        raise RuntimeError("recorder acknowledgement timed out")
    events = []
    owner = SimpleNamespace(status={"connection": "connected"}, create_task=fail, event=events.append)
    with TestClient(create_app(owner)) as client:
        headers = {"X-YAM-Control": "1"}
        assert client.post("/tasks", headers=headers, json={"name": "n", "instruction": "i", "task": "pick lego"}).status_code == 409
        assert client.get("/status").json()["connection"] == "connected"
        assert client.post("/event/hold", headers=headers).status_code == 200
        assert events == ["hold"]


def test_busy_task_edit_fails_fast_and_status_has_no_file_io(tmp_path):
    service = Workbench(SimpleNamespace(mode="collect", mock=True, url=None, task_root=tmp_path))
    try:
        service._lock.acquire()
        try:
            start = time.monotonic()
            with pytest.raises(ValueError, match="本次未提交"):
                service.create_task("new", "new", "pick lego")
            assert time.monotonic() - start < 0.1
        finally:
            service._lock.release()
        service._station_inventory = lambda: (_ for _ in ()).throw(OSError("disk unavailable"))
        assert service.status["connection"] == "disconnected"
    finally:
        service.close()


def test_control_never_requires_or_creates_token(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("YAM_CONTROL_TOKEN", "obsolete-setting")
    owner = SimpleNamespace(status={})
    app = create_app(owner, control_access=True)
    path = tmp_path / "data/workstation/control.token"
    assert not path.exists()
    with TestClient(app) as client:
        assert client.get("/status").json()["control_auth_required"] is False
        assert client.post("/heartbeat", headers={"X-YAM-Control": "1", "X-YAM-Session": "a"}).status_code == 200
