import math
import time
from types import SimpleNamespace as NS

import numpy as np
import pytest

from yam_abc_reproduce.config import StationConfig
from yam_abc_reproduce.hil.device_health import MOTION_EVENTS, DeviceHealth
from yam_abc_reproduce.hil.run import Runtime
from yam_abc_reproduce.hil.station import StationIO
from yam_abc_reproduce.robot.yam_adapter import _motor_health
from yam_abc_reproduce.runtime import build_arm_units


def arms():
    return [dict(label=name, running=True, feedback_age_s=.005, motors=[
        dict(joint=f"J{i+1}", error_code=1) for i in range(6)])
        for name in ("左 Follower", "右 Follower", "左 Leader", "右 Leader")]


def sample(health, now, *, measured=0, target=.4, enabled=True, index=5, report=None):
    q, h = np.zeros(14), np.zeros(14)
    q[index] = measured
    health.update(arms() if report is None else report, now=now, follower=q, leader=h)
    command = q.copy()
    command[index] = target
    health.submitted(command, h, enabled=[enabled, enabled, False, False], now=now)


@pytest.mark.parametrize("code,text", [(0,"未使能"),(8,"过压"),(9,"欠压"),(10,"过流"),
                                      (11,"驱动过温"),(12,"电机过温"),(13,"通信丢失"),(14,"过载")])
def test_preflight_reports_exact_arm_joint_and_fault(code, text):
    health = DeviceHealth()
    report = arms()
    report[0]["motors"][5]["error_code"] = code
    sample(health, 2, report=report)
    assert f"左 Follower J6：{text}" in health.blocking_reason(now=2)


def test_stale_missing_and_stopped_are_not_healthy_even_last_motor_code_normal():
    health = DeviceHealth()
    assert health.blocking_reason(now=0)
    report = arms()
    report[0].update(running=False, feedback_age_s=.8)
    sample(health, 1, report=report)
    assert "线程已停止" in health.blocking_reason(now=1)
    assert "反馈超时" in health.blocking_reason(now=1)
    sample(health, 2)
    assert health.blocking_reason(now=2) is None
    assert "过期" in health.blocking_reason(now=2.3)
    sample(health, 3, report=[])
    assert not health.snapshot(now=3)["ok"]


def test_stuck_joint_latches_and_only_explicit_fresh_ack_clears():
    health = DeviceHealth()
    for tick in range(40):
        sample(health, tick / 30)
    assert health.tracking_fault["joint"] == "J6"
    assert health.tracking_fault["arm"] == "左 Follower"
    assert health.tracking_fault["error_deg"] > 10
    sample(health, 1.4, target=0)
    assert health.tracking_fault  # HOLD/zero error cannot hide the prior incident.
    with pytest.raises(ValueError, match="过期"):
        health.acknowledge(now=3)
    assert health.tracking_fault
    health.acknowledge(now=1.4)
    assert not health.tracking_fault and health._previous is None


@pytest.mark.parametrize("scenario", ["below_threshold", "transient", "moving", "manual", "gripper", "loop_gap", "direction_change"])
def test_does_not_trip_on_normal_lag_or_unmonitored_feedback(scenario):
    health = DeviceHealth()
    for tick in range(50):
        now = tick / 30 if scenario != "loop_gap" else tick / 2
        measured = tick * .002 if scenario == "moving" else 0
        target = measured + (.4 if scenario != "below_threshold" else math.radians(9))
        if scenario == "transient" and tick > 20:
            target = 0
        if scenario == "direction_change":
            target *= (-1) ** (tick // 20)
        sample(health, now, measured=measured, target=target, enabled=scenario != "manual",
               index=6 if scenario == "gripper" else 5)
    assert health.tracking_fault is None


def test_adapter_health_reads_published_values_without_lock_or_can_call(monkeypatch):
    class Forbidden:
        def __enter__(self):
            raise AssertionError("health must not acquire the CAN lock")
    frame = NS(id=6, error_code="0xa", temperature_mos=44, temperature_rotor=43)
    chain = NS(state=[frame], running=True, state_lock=Forbidden(),
               _control_thread=NS(is_alive=lambda: True))
    robot = NS(motor_chain=chain, _joint_state=NS(timestamp=time.time()),
               _server_thread=NS(is_alive=lambda: True))
    monkeypatch.setattr("yam_abc_reproduce.robot.yam_adapter.time.monotonic", lambda: 1.0)
    result = _motor_health(robot)
    assert result["running"] and result["motors"][0]["error_code"] == 10
    assert result["motors"][0]["temp_mos_c"] == 44
    monkeypatch.setattr("yam_abc_reproduce.robot.yam_adapter.time.monotonic", lambda: 1.5)
    robot._joint_state.timestamp = time.time()
    assert _motor_health(robot)["feedback_age_s"] >= .5  # CAN publication froze.
    chain.state = [frame]  # Producer publishes a new complete frame list.
    assert _motor_health(robot)["feedback_age_s"] < .25
    chain.running = False
    assert not _motor_health(robot)["running"]


@pytest.mark.parametrize("event", sorted(MOTION_EVENTS - {"manual_ready"}))
def test_start_events_reject_without_health(event):
    cameras = [NS(role=role) for role in ("top", "left", "right")]
    runtime = Runtime(NS(), cameras, None, NS(recording=False, saving=False), mode="teleop")
    with pytest.raises(ValueError, match="设备健康"):
        runtime.event(event)
    assert runtime.events.empty()


def test_mock_station_reports_simulation_without_invented_temperatures():
    io = StationIO(build_arm_units(StationConfig(), mock=True), mock=True)
    try:
        report = io.device_health()
        assert len(report) == 4
        assert report[0]["motors"][0]["temp_mos_c"] is None
    finally:
        io.close()


def test_health_api_is_read_only_and_available_without_connection():
    from fastapi.testclient import TestClient

    from yam_abc_reproduce.hil.web import create_app
    calls = []
    owner = NS(status={"connection": "disconnected"}, event=calls.append,
               check_device_health=lambda: {"ok": False, "reason": "机械臂未连接", "arms": []})
    with TestClient(create_app(owner)) as client:
        response = client.get("/device/health")
    assert response.status_code == 200 and response.json()["ok"] is False
    assert calls == []


@pytest.mark.parametrize("connection,phase,age", [("disconnected","hold",0),
                                                  ("connected","fault",0),
                                                  ("connected","hold",1)])
def test_page_never_reuses_green_health_after_disconnect_fault_or_stall(tmp_path, connection, phase, age):
    from yam_abc_reproduce.hil.workbench import Workbench
    owner = Workbench(NS(mode="collect", mock=True, url=None, task_root=tmp_path / "tasks"))
    try:
        owner.state = connection
        owner.runtime = NS(status={"updated_at": time.monotonic()-age, "phase": phase,
                                  "device_health": {"ok": True}},
                           maintenance=NS(factory_zero=False), task_switching=False)
        assert not owner.status["device_health"]["ok"]
        assert owner.check_device_health()["reason"]
    finally:
        owner.runtime = None
        owner.close()


def test_following_fault_never_downgrades_emergency_stop_to_ordinary_hold(tmp_path):
    from yam_abc_reproduce.hil.recording import RecordingSession
    io = StationIO(build_arm_units(StationConfig(), mock=True), mock=True)
    rec = RecordingSession(tmp_path / "stop", mode="collect")
    cameras = [NS(role=role, history=lambda: []) for role in ("top", "left", "right")]
    runtime = Runtime(io, cameras, None, rec, mode="collect")
    runtime.device_health.tracking_fault = {"message": "injected non-following warning"}
    runtime.session.arbiter.start(np.zeros(14), np.zeros(14))
    runtime.emergency.set()
    try:
        runtime.run(duration=.08)
        assert runtime.maintenance.latched
        assert runtime.status["stop_latched"]
    finally:
        rec.close()
        io.close()


@pytest.mark.parametrize("session", [False, True])
def test_runtime_stuck_j6_pauses_freezes_leaders_and_persists_diagnostic(tmp_path, session):
    import json

    from yam_abc_reproduce.camera.mock_camera import MockCamera
    from yam_abc_reproduce.camera.worker import CameraWorker
    from yam_abc_reproduce.hil.recording import Recorder, RecordingSession
    from yam_abc_reproduce.hil.storage import read_rows

    io = StationIO(build_arm_units(StationConfig(), mock=True), mock=True)
    io._mock_leaders[5] = .4
    robot = io.units[0].robot
    write = robot.command_joint_pos
    def stuck_j6(q):
        q = q.copy()
        q[5] = 0
        write(q)
    robot.command_joint_pos = stuck_j6
    cameras = [CameraWorker(MockCamera(role, role, width=64, height=48))
               for role in ("top", "left", "right")]
    path = tmp_path / "episode"
    rec = RecordingSession(path, mode="collect") if session else Recorder(path, fps=30)
    runtime = Runtime(io, cameras, None, rec, mode="collect" if session else "teleop")
    try:
        for camera in cameras:
            camera.start()
        if session:
            rec.start_episode()
        status = runtime.run(duration=1.8, auto_start=True)
        assert status["phase"] == "hold", status
        assert status["device_health"]["tracking_fault"]["joint"] == "J6"
        assert status["leader_locked"] and io._manual is False
        with pytest.raises(ValueError, match="疑似不跟随"):
            runtime.event("start")
        rec.close()
        if session:
            event = json.loads((path / "policy_events.jsonl").read_text().splitlines()[0])
            manifest = json.loads((path / "episode_000001" / "manifest.json").read_text())
            assert event["event"] == "device_health_fault" and event["fault"]["joint"] == "J6"
            assert event["episode_id"] == manifest["episode_id"]
            assert manifest["outcome"] == "aborted"
        else:
            assert any(row["device_tracking_fault"] for row in read_rows(path))
    finally:
        rec.close()
        for camera in cameras:
            camera.stop()
        io.close()
