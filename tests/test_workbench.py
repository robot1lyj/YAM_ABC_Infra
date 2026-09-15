import json
import queue
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from yam_abc_reproduce.hil.jog import Jog
from yam_abc_reproduce.hil.maintenance import Maintenance
from yam_abc_reproduce.hil.web import create_app


def test_jog_bounded_steps_cancel_and_no_queue_growth():
    jog = Jog()
    jog.request("left", 0, np.deg2rad(2))
    with pytest.raises(queue.Full):
        jog.request("right", 0, 0.01)
    q = np.zeros(14)
    step = jog.step(q, allowed=True, now=1, dt=1 / 30)
    assert step[0] == pytest.approx(0.1 / 30)
    assert jog.step(q, allowed=False, now=1.1, dt=0.03) is None
    assert jog.target is None
    assert jog.step(q, allowed=True, now=1.2, dt=0.03) is None
    with pytest.raises(ValueError):
        jog.request("left", 0, 3)


def test_home_exclusive_coordinated_and_gripper_retained():
    m = Maintenance()
    target, lead = np.zeros(14), np.zeros(14)
    target[0], lead[7] = 0.3, 0.6
    m.capture(target, lead)
    q, h = np.zeros(14), np.zeros(14)
    q[[6, 13]] = [0.4, 0.8]
    assert m.command("home", q, h, now=0, paused=True) == "hold"
    assert m.command("start", q, h, now=0.01, paused=True) == "hold"
    q1, h1 = m.step(q, h, now=0.1, dt=0.1)
    assert q1[0] == pytest.approx(0.006)
    assert h1[7] == pytest.approx(0.012)
    np.testing.assert_array_equal(q1[[6, 13]], q[[6, 13]])
    m.command("hold", q1, h1, now=0.2, paused=True)
    assert m.step(q1, h1, now=0.3, dt=0.1) is None
    assert m.state == "idle"


def test_stop_freezes_four_arms_and_reset_never_resumes():
    m = Maintenance()
    q, h = np.zeros(14), np.full(14, 0.2)
    m.capture(q, h)
    m.command("home", q, h, now=0, paused=True)
    m.command("stop", q, h, now=1, paused=False)
    assert m.latched and m.state == "idle"
    assert m.command("start", q, h, now=2, paused=True) == "hold"
    frozen_q, frozen_h = m.step(q + 0.1, h + 0.1, now=2, dt=0.03)
    np.testing.assert_array_equal(frozen_q, q)
    np.testing.assert_array_equal(frozen_h, h)
    assert m.command("reset_stop", q, h, now=3, paused=True) == "hold"
    assert not m.latched and m.state == "idle"
    assert m.step(q, h, now=3, dt=0.03) is None


def test_home_requires_teaching_and_pause_and_has_timeout():
    m = Maintenance()
    q = np.zeros(14)
    with pytest.raises(ValueError):
        m.command("home", q, q, now=0, paused=True)
    with pytest.raises(ValueError):
        m.command("gravity", q, q, now=0, paused=False)
    m.capture(q, q)
    m.command("home", q, q, now=0, paused=True)
    assert m.step(q, q, now=61, dt=0.03) is None
    assert m.error and m.state == "idle"


def test_gravity_owns_controls_until_explicit_hold():
    m = Maintenance()
    q = np.zeros(14)
    m.command("gravity", q, q, now=0, paused=True)
    assert m.command("start", q, q, now=1, paused=True) == "hold"
    assert m.state == "gravity"
    m.command("hold", q, q, now=2, paused=True)
    assert m.state == "idle"


def test_api_same_origin_controls_and_validation():
    events = []
    runtime = SimpleNamespace(status={"connection": "disconnected"}, event=events.append)
    with TestClient(create_app(runtime)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/assets/app.js").status_code == 200
        assert client.post("/event/start").status_code == 403
        headers = {"X-YAM-Control": "1"}
        assert (
            client.post(
                "/event/start", headers={**headers, "Origin": "https://evil.example"}
            ).status_code
            == 403
        )
        assert (
            client.post("/event/start", headers={**headers, "Host": "evil.example"}).status_code
            == 400
        )
        assert client.post("/event/start", headers=headers).status_code == 200
        assert events == ["start"]
        assert (
            client.post(
                "/jog", headers=headers, json={"arm": "left", "joint": 1.5, "delta": 0.1}
            ).status_code
            == 422
        )


def test_dashboard_accepts_explicit_ipc_lan_host_without_relaxing_origin():
    events = []
    runtime = SimpleNamespace(
        status={"connection": "disconnected"},
        event=events.append,
        args=SimpleNamespace(web_host="192.168.110.140", web_allowed_host=[]),
    )
    with TestClient(create_app(runtime), base_url="http://192.168.110.140:8766") as client:
        headers = {"X-YAM-Control": "1", "Origin": "http://192.168.110.140:8766"}
        assert client.get("/status").status_code == 200
        assert client.post("/event/start", headers=headers).status_code == 200
        assert (
            client.post(
                "/event/start",
                headers={**headers, "Origin": "http://192.168.110.141:8766"},
            ).status_code
            == 403
        )
    assert events == ["start"]


def test_browser_service_initialization_never_constructs_devices(tmp_path, monkeypatch):
    from yam_abc_reproduce.hil import run
    from yam_abc_reproduce.hil.workbench import Workbench

    monkeypatch.setattr(run, "build_arm_units", lambda *a, **k: pytest.fail("device constructed"))
    service = Workbench(
        SimpleNamespace(mode="collect", mock=True, url=None, task_root=tmp_path / "tasks")
    )
    try:
        assert service.status["connection"] == "disconnected"
        assert service.runtime is None
        service.event("mode:hil")
        assert service.mode == "hil"
    finally:
        service.close()


def test_initialization_preflight_is_read_only_and_exposes_inventory(tmp_path, monkeypatch):
    from yam_abc_reproduce.hil import run
    from yam_abc_reproduce.hil.workbench import Workbench

    monkeypatch.setattr(run, "build_arm_units", lambda *a, **k: pytest.fail("device constructed"))
    service = Workbench(
        SimpleNamespace(
            mode="collect",
            mock=True,
            url=None,
            station="configs/station_hil.yaml",
            task_root=tmp_path / "tasks",
        )
    )
    try:
        result = service.initialization_preflight()
        assert result["ok"] is True
        assert {x["channel"] for x in result["can"]} == {
            "can_left",
            "can_right",
            "can_lead_l",
            "can_lead_r",
        }
        inventory = service.status["initialization"]["inventory"]
        assert [x["gripper"] for x in inventory["followers"]] == [
            "linear_4310",
            "linear_4310",
        ]
        assert {x["role"] for x in inventory["cameras"]} == {"top", "left", "right"}
        assert service.runtime is None and service.state == "disconnected"
    finally:
        service.close()


def test_initialization_connect_does_not_require_collection_task(tmp_path, monkeypatch):
    from yam_abc_reproduce.hil.workbench import Workbench

    monkeypatch.chdir(tmp_path)
    station = Path(__file__).parents[1] / "configs/station_hil.yaml"
    service = Workbench(
        SimpleNamespace(
            mode="hil",
            mock=True,
            url=None,
            station=str(station),
            task_root=tmp_path / "tasks",
            output=None,
            baseline=False,
            segment_seconds=60,
            min_free_gb=0.01,
        )
    )
    try:
        assert service.initialization_preflight()["ok"] is True
        service.connect_cameras()
        deadline = time.monotonic() + 5
        while service.camera_state == "connecting" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert service.camera_state == "connected", service.status
        service.connect(ready=True, initialize=True)
        deadline = time.monotonic() + 5
        while service.state == "connecting" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert service.state == "connected", service.status
        assert service.initializing is True
        assert service.runtime.status["mode"] == "collect"
        assert service.selected_task is None
        with pytest.raises(ValueError, match="初始化会话"):
            service.event("start")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            cameras = service.status.get("cameras", [])
            if len(cameras) == 3 and all(camera.get("healthy") for camera in cameras):
                break
            time.sleep(0.02)
        report = service.complete_initialization(gravity_checked=True, leader_checked=True)
        assert report["gravity_checked"] and report["leader_checked"]
        saved = json.loads((tmp_path / "data/workstation/initialization_mock.json").read_text())
        assert saved["station_sha256"] == report["station_sha256"]
        assert len(saved["gripper_measurements"]) == 2
        service.disconnect(supported=True)
    finally:
        service.close()


def test_preview_process_jpeg_bounded_and_no_input_mutation():
    import io

    import av

    from yam_abc_reproduce.hil.preview import Preview

    preview = Preview()
    rgb = np.full((64, 64, 3), [255, 10, 20], dtype=np.uint8)
    original = rgb.copy()
    try:
        assert preview.submit({"top": rgb, "left": rgb, "right": rgb})
        deadline = time.monotonic() + 8
        result = None
        while result is None and time.monotonic() < deadline:
            result = preview.poll()
            time.sleep(0.05)
        assert result is not None
        assert set(result[1]) == {"top", "left", "right"}
        with av.open(io.BytesIO(result[1]["top"])) as video:
            frame = next(video.decode(video=0)).to_ndarray(format="rgb24")
            assert frame.shape == (360, 480, 3) and frame[:, :, 0].mean() > 240
        np.testing.assert_array_equal(rgb, original)
        assert preview.process.is_alive()
    finally:
        preview.close()
    assert not preview.process.is_alive()


def test_runtime_emergency_reset_jog_and_home(tmp_path):
    from yam_abc_reproduce.config import build_station_config
    from yam_abc_reproduce.hil.recording import RecordingSession
    from yam_abc_reproduce.hil.run import Runtime
    from yam_abc_reproduce.hil.station import StationIO
    from yam_abc_reproduce.runtime import build_arm_units

    cfg = build_station_config("configs/station_hil.yaml")
    io = StationIO(build_arm_units(cfg, mock=True), mock=True)
    rec = RecordingSession(tmp_path / "session", mode="collect")
    runtime = Runtime(
        io,
        [SimpleNamespace(role=r, history=lambda: []) for r in ("top", "left", "right")],
        None,
        rec,
        mode="collect",
    )
    thread = threading.Thread(target=runtime.run)
    thread.start()

    def wait(predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert predicate(), runtime.status

    try:
        wait(lambda: runtime.status["tick"] > 0)
        runtime.event("capture_home")
        wait(lambda: runtime.maintenance.ready is not None)
        runtime.request_jog("left", 0, np.deg2rad(2))
        wait(lambda: runtime.status["follower_state"][0] > 0.02)
        runtime.event("stop")
        wait(lambda: runtime.status.get("stop_latched"))
        frozen = np.array(runtime.status["follower_state"])
        with pytest.raises(ValueError):
            runtime.event("start")
        time.sleep(0.1)
        np.testing.assert_allclose(runtime.status["follower_state"], frozen, atol=0.005)
        runtime.event("reset_stop")
        wait(lambda: not runtime.status.get("stop_latched"))
        assert runtime.status["phase"] == "hold"
        runtime.event("home")
        wait(lambda: runtime.status.get("maintenance") == "homing")
        wait(lambda: runtime.status.get("maintenance") == "idle")
        assert abs(runtime.status["follower_state"][0]) < 0.016
        assert not rec.recording
    finally:
        runtime.event("quit")
        thread.join(3)
        rec.close()
        io.close()
    assert not thread.is_alive()


def test_gravity_adapter_retains_gripper_and_does_not_overwrite_native_gains():
    from yam_abc_reproduce.robot.yam_adapter import YamRobot

    robot = YamRobot.__new__(YamRobot)
    calls = []
    robot._robot = SimpleNamespace(
        use_gravity_comp=True,
        _kp=np.full(7, 10.0),
        _kd=np.ones(7),
        _grav_comp_kd=np.full(7, 0.4),
        command_joint_state=calls.append,
    )
    robot._n, robot._g_closed, robot._g_open = 6, 0.0, 1.0
    q = np.full(7, 0.5)
    robot.gravity_compensate(q)
    np.testing.assert_array_equal(calls[0]["kp"][:6], np.zeros(6))
    assert calls[0]["kp"][6] == 10
    assert calls[0]["pos"][6] == 0.5
    np.testing.assert_array_equal(robot._robot._kp, np.full(7, 10.0))


def test_watchdog_is_independent_of_preview_and_requests_hold(tmp_path):
    from yam_abc_reproduce.hil.workbench import Workbench

    service = Workbench(
        SimpleNamespace(mode="collect", mock=True, url=None, task_root=tmp_path / "tasks")
    )
    events = []
    runtime = SimpleNamespace(
        event=events.append, cameras=[], status={}, recorder=SimpleNamespace(episodes=[])
    )
    try:
        service.preview_enabled = False
        service.runtime = runtime
        service._heartbeat = time.monotonic() - 4
        deadline = time.monotonic() + 1
        while "hold" not in events and time.monotonic() < deadline:
            time.sleep(0.02)
        assert "hold" in events and service._operator_lost
        service.heartbeat()
        assert not service._operator_lost
        assert "start" not in events
    finally:
        service.close()


def test_moment_merge_is_exact_and_independent():
    from yam_abc_reproduce.hil.lerobot_export import Moments

    rng = np.random.default_rng(9)
    values = rng.random((800, 3))
    direct = Moments()
    direct.add(values)
    a, b = Moments(), Moments()
    first = values[:300]
    args = (len(first), first.mean(0), first.var(0), first.min(0), first.max(0))
    a.merge(*args)
    b.merge(*args)
    b.add(values[300:])
    np.testing.assert_allclose(a.mean, first.mean(0))
    for key in ("mean", "std", "min", "max"):
        np.testing.assert_allclose(b.result()[key], direct.result()[key], atol=1e-14)


def test_gravity_retains_entry_gripper_target_when_feedback_drifts(tmp_path):
    from yam_abc_reproduce.hil.recording import RecordingSession
    from yam_abc_reproduce.hil.run import Runtime

    class IO:
        mock = True
        count = 0
        commands = []

        def read(self):
            self.count += 1
            q = np.zeros(14)
            q[[6, 13]] = 0.4 if self.count == 1 else 0.6
            return q, q.copy(), [[False, False]] * 2, [0.0] * 4

        def apply(self, decision, q, leader, **kwargs):
            self.commands.append((decision.action.copy(), kwargs))
            return decision.action.copy(), {}

        def hold(self):
            return []

    rec = RecordingSession(tmp_path / "gravity", mode="collect")
    io = IO()
    runtime = Runtime(
        io,
        [SimpleNamespace(role=r, history=lambda: []) for r in ("top", "left", "right")],
        None,
        rec,
        mode="collect",
    )
    try:
        runtime.event("gravity")
        runtime.run(duration=0.15)
        assert len(io.commands) >= 2
        assert io.commands[1][1]["gravity"] is True
        assert io.commands[1][0][6] < 0.6
        np.testing.assert_allclose(runtime.maintenance.grippers, [0.4, 0.4])
    finally:
        rec.close()


def test_recording_failure_holds_arms_without_faulting_session(tmp_path, monkeypatch):
    from yam_abc_reproduce.hil.recording import RecordingSession
    from yam_abc_reproduce.hil.run import Runtime

    class IO:
        mock = True

        def __init__(self):
            self.holds = 0
            self.commands = 0

        def read(self):
            q = np.zeros(14)
            q[[6, 13]] = 0.5
            return q, q.copy(), [[False, False]] * 2, [0.0] * 4

        def apply(self, decision, q, leader, **kwargs):
            self.commands += 1
            return decision.action.copy(), {}

        def hold(self):
            self.holds += 1
            return []

    rec = RecordingSession(tmp_path / "recording-failure", mode="collect")
    original_submit = rec.submit
    submitted = 0

    def fail_after_two_rows(row, images):
        nonlocal submitted
        if rec.recording:
            submitted += 1
            if submitted == 3:
                rec.error = "episode queue full"
                return False
        return original_submit(row, images)

    monkeypatch.setattr(rec, "submit", fail_after_two_rows)
    io = IO()
    runtime = Runtime(
        io,
        [SimpleNamespace(role=r, history=lambda: []) for r in ("top", "left", "right")],
        None,
        rec,
        mode="collect",
    )
    try:
        rec.start_episode()
        runtime.event("start")
        runtime.run(duration=0.2)
        assert runtime.status["phase"] == "hold"
        assert runtime.status["recording_error"] == "episode queue full"
        assert runtime.status["error"] is None
        assert not rec.recording
        assert io.holds >= 2
        assert io.commands >= 5  # Control keeps ticking after the writer fails.
        with pytest.raises(ValueError, match="录制已中断"):
            runtime.event("record")
        with pytest.raises(ValueError, match="录制已中断"):
            runtime.event("start")

        runtime.event("mode:teleop")
        runtime.run(duration=0.08)
        assert runtime.status["mode"] == "teleop"
        assert runtime.status["phase"] == "hold"
        runtime.event("start")
        runtime.run(duration=0.08)
        assert runtime.status["phase"] == "human"
        assert runtime.status["recording_error"] == "episode queue full"
    finally:
        rec.close("aborted")
    assert rec.episodes[0]["outcome"] == "aborted"


def test_full_episode_queue_aborts_in_writer_without_blocking_control(tmp_path, monkeypatch):
    from yam_abc_reproduce.hil.recording import RecordingSession

    gate = threading.Event()
    real_run = RecordingSession._run

    def delayed_run(self):
        assert gate.wait(2)
        real_run(self)

    monkeypatch.setattr(RecordingSession, "_run", delayed_run)
    rec = RecordingSession(tmp_path / "full-episode-queue", mode="collect", capacity=1)
    try:
        rec.start_episode()
        assert not rec.submit({"tick": 0}, {})
        assert rec.error == "episode queue full"
        rec.abort_episode()
        assert not rec.recording
        assert rec.submit({"tick": 1}, {})  # The control loop no longer enters a writer queue.
    finally:
        gate.set()
        rec.close("aborted")
    assert rec.episodes[0]["outcome"] == "aborted"
