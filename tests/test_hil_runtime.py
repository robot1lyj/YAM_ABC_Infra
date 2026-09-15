import json
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from yam_abc_reproduce.camera.mock_camera import MockCamera
from yam_abc_reproduce.camera.worker import CameraWorker
from yam_abc_reproduce.config import StationConfig
from yam_abc_reproduce.hil.core import Arbiter, Mode, Phase
from yam_abc_reproduce.hil.observation import Observations
from yam_abc_reproduce.hil.policy import PolicyWorker
from yam_abc_reproduce.hil.recording import Recorder
from yam_abc_reproduce.hil.run import MockPolicy, Runtime, validate_station
from yam_abc_reproduce.hil.session import Session
from yam_abc_reproduce.hil.station import StationIO
from yam_abc_reproduce.hil.storage import read_rows
from yam_abc_reproduce.runtime import build_arm_units


def test_async_chunks_align_to_observation_time_and_replan_during_execution():
    q = np.zeros(14)
    a = Arbiter(Mode.HIL, streaming=True, action_dt=0.1, max_action_age=5)
    a.start(q)
    t = a.request(1, 0.1, observed_at=0)
    rows = np.zeros((50, 14))
    rows[:, 0] = np.arange(50) * 0.01
    assert a.accept(t, rows, 0.21)
    d = a.step(q, q, now=0.21, dt=0.03, leader_ready=True)
    assert d.action_index == 2 and d.policy_action[0] == 0.02
    assert a.request(2, 0.3, observed_at=0.29) is not None
    a.takeover(q, q)
    assert a.pending is None and a.phase == Phase.TAKEOVER


def test_model_interval_does_not_accelerate_with_control_ticks():
    q = np.zeros(14)
    a = Arbiter(Mode.INFERENCE, streaming=True, action_dt=0.1)
    a.start(q)
    t = a.request(1, 0)
    a.accept(t, np.zeros((50, 14)), 0.01)
    indices = [
        a.step(q, q, now=n, dt=0.02, leader_ready=True).action_index
        for n in (0.02, 0.04, 0.06, 0.12)
    ]
    assert indices == [0, 0, 0, 1]


def test_mode_switch_rejects_old_reply_and_stays_held():
    q = np.zeros(14)
    a = Arbiter(Mode.HIL)
    a.start(q)
    t = a.request(1, 0)
    Session(a).tick(q, q, now=0.01, dt=0.03, observation_id=2, event="mode:teleop")
    assert a.mode == Mode.TELEOP and a.phase == Phase.HOLD
    assert not a.accept(t, np.zeros((50, 14)), 0.02)


def test_observation_interpolates_state_and_reports_arrival_skew():
    im = np.zeros((8, 8, 3), np.uint8)

    def cam(role, t):
        frame = SimpleNamespace(images={"rgb": im}, meta={"host_received_at": t, "sequence": 1})
        return SimpleNamespace(role=role, history=lambda: [frame])

    obs = Observations([cam("top", 1), cam("left", 1.02), cam("right", 1.05)])
    obs.add_state(0.9, np.zeros(14))
    obs.add_state(1.1, np.ones(14))
    _, t, payload, _, quality = obs.snapshot(1.1, "test")
    np.testing.assert_allclose(payload["observation.state"], 0.5)
    assert t == 1 and quality["sync_warning"]
    assert obs.snapshot(1.6, "test") is None


def test_recorder_streams_three_videos_and_matching_json_rows(tmp_path):
    import av

    r = Recorder(tmp_path / "episode", capacity=8)
    im = np.zeros((32, 32, 3), np.uint8)
    for i in range(3):
        assert r.submit(
            {"tick": i, "policy_valid": False, "policy_action": None},
            {role: im for role in ("top", "left", "right")},
        )
    r.close("success")
    assert not r.error
    manifest = json.loads((r.path / "manifest.json").read_text())
    assert manifest["steps"] == 3 and manifest["outcome"] == "success"
    rows = list(read_rows(r.path))
    for role in ("top", "left", "right"):
        with av.open(str(r.path / "segment_000000" / f"{role}.mp4")) as video:
            assert len(list(video.decode(video=0))) == 3
        assert [row["video_indices"][role] for row in rows] == [0, 1, 2]


def test_recording_backpressure_is_explicit(tmp_path, monkeypatch):
    gate = threading.Event()
    real_run = Recorder._run

    def delayed_run(self):
        assert gate.wait(2)
        real_run(self)

    monkeypatch.setattr(Recorder, "_run", delayed_run)
    r = Recorder(tmp_path / "episode", capacity=1)
    try:
        assert r.submit({"tick": 0}, {})
        assert not r.submit({"tick": 1}, {})
        assert r.error == "recording queue full"
    finally:
        gate.set()
        r.close()
    assert json.loads((r.path / "manifest.json").read_text())["outcome"] == "aborted"


def test_runtime_end_to_end_takeover_resume_and_recording(tmp_path):
    cfg = StationConfig()
    io = StationIO(build_arm_units(cfg, mock=True), mock=True)
    cameras = [
        CameraWorker(MockCamera(role, role, width=32, height=32))
        for role in ("top", "left", "right")
    ]
    rec = Recorder(tmp_path / "episode")
    worker = PolicyWorker(MockPolicy())
    try:
        for c in cameras:
            c.start()
        run = Runtime(io, cameras, worker, rec)
        run.event("success")
        assert run.outcome == "unknown"  # consumed in the control owner
        result = run.run(duration=2.5, auto_start=True, demo=True)
        rec.close(run.outcome)
        assert not result["error"] and result["phase"] == "policy"
        rows = list(read_rows(rec.path))
        assert {"policy", "human", "hold"} <= {r["source"] for r in rows}
        human = [r for r in rows if r["source"] == "human"]
        assert all(not r["policy_valid"] and r["policy_action"] is None for r in human)
        assert any(r["expert_valid"] for r in human)
        epochs = [r["epoch"] for r in rows]
        assert epochs == sorted(epochs)
        assert any(r.get("policy_reply") for r in rows)
    finally:
        for c in cameras:
            c.stop()
        worker.close()
        io.close()
        if rec._thread.is_alive():
            rec.close("aborted")


def test_official_leader_switches_gains_and_clears_active_commands(monkeypatch):
    from yam_abc_reproduce.robot import yam_adapter as adapter

    class Device:
        _kp = np.ones(6) * 10
        _kd = np.ones(6) * 2

        def __init__(self):
            self.calls = []

        def update_kp_kd(self, kp, kd):
            self.calls.append(("gains", kp.copy(), kd.copy()))

        def get_joint_pos(self):
            return np.ones(6) * 0.1

        def command_joint_pos(self, q):
            self.calls.append(("position", q.copy()))

        def enter_gravity_comp_idle(self):
            self.calls.append(("gravity",))

    device = Device()
    monkeypatch.setattr(adapter, "_build_yam", lambda *args, **kw: device)
    leader = adapter.YamLeaderArm("unused")
    leader.set_manual_control(False, 0.2)
    np.testing.assert_allclose(device.calls[-2][1], 2)
    assert device.calls[-1][0] == "position"
    leader.set_manual_control(True)
    np.testing.assert_array_equal(device.calls[-2][1], np.zeros(6))
    assert device.calls[-1][0] == "gravity"
    before = len(device.calls)
    leader.set_manual_control(True)
    assert len(device.calls) == before


def test_i2rt_hil_snapshot_copies_one_published_state_without_lock():
    from yam_abc_reproduce.robot import yam_adapter as adapter

    class LockThatMustNotBeEntered:
        def __enter__(self):
            raise AssertionError("HIL snapshot waited on i2rt state lock")

    state = SimpleNamespace(pos=np.arange(7, dtype=float), timestamp=time.time())
    device = SimpleNamespace(
        _server_thread=SimpleNamespace(is_alive=lambda: True),
        _state_lock=LockThatMustNotBeEntered(),
        _joint_state=state,
    )
    robot = adapter.YamRobot.__new__(adapter.YamRobot)
    robot._robot, robot._n = device, 6
    robot._g_closed, robot._g_open = 6.0, 7.0

    pos, age = robot.hil_read()
    state.pos[:] = -1  # returned snapshot must not alias the next producer update
    np.testing.assert_array_equal(pos, np.r_[np.arange(6, dtype=float), 0.0])
    assert 0 <= age < 0.1


def test_yam_gripper_commands_stay_inside_calibrated_hard_stops():
    from yam_abc_reproduce.robot import yam_adapter as adapter

    class Robot:
        use_gravity_comp = True
        _kp = np.ones(7)
        _kd = np.ones(7)
        _grav_comp_kd = np.ones(7)

        def command_joint_pos(self, value):
            self.position = np.asarray(value).copy()

        def command_joint_state(self, value):
            self.state = value

    robot = adapter.YamRobot.__new__(adapter.YamRobot)
    robot._robot, robot._n = Robot(), 6
    robot._g_closed, robot._g_open = 0.0, 1.0
    robot._gripper_command_margin = adapter.GRIPPER_ENDPOINT_MARGIN

    robot.command_joint_pos(np.r_[np.zeros(6), 0.0])
    assert robot._robot.position[-1] == adapter.GRIPPER_ENDPOINT_MARGIN
    robot.command_joint_pos(np.r_[np.zeros(6), 1.0])
    assert robot._robot.position[-1] == 1.0 - adapter.GRIPPER_ENDPOINT_MARGIN
    robot.gravity_compensate(np.r_[np.zeros(6), 0.0])
    assert robot._robot.state["pos"][-1] == adapter.GRIPPER_ENDPOINT_MARGIN


def test_partial_motor_failure_attempts_station_hold():
    cfg = StationConfig()
    io = StationIO(build_arm_units(cfg, mock=True), mock=True)
    called = []

    def fail(_):
        called.append("left")
        raise RuntimeError("CAN failure")

    io.units[0].robot.command_joint_pos = fail
    io.units[1].robot.command_joint_pos = lambda _: called.append("right")
    errors = io.hold()
    assert errors and called == ["left", "right"]


def test_real_station_reads_four_independent_devices_in_parallel():
    """Each real device has its own CAN bus/state lock, so read latency is the
    slowest snapshot rather than the sum of all four waits. Motor writes remain
    ordered in StationIO.apply()."""
    delay = 0.04
    units = []
    for index, name in enumerate(("left", "right")):
        follower = np.full(7, index + 1.0)
        leader = np.full(7, index + 3.0)
        follower[-1] = 0.2 + 0.2 * index
        leader[-1] = 0.3 + 0.3 * index

        def follower_read(value=follower):
            time.sleep(delay)
            return value.copy()

        def leader_read(value=leader, age=0.003 + index, side=index):
            time.sleep(delay)
            return value.copy(), [side == 0, side == 1], age

        robot = SimpleNamespace(
            joint_limits=lambda: np.tile([-3.0, 3.0], (6, 1)),
            get_joint_pos=follower_read,
            feedback_age=lambda age=0.001 + index: age,
            command_joint_pos=lambda _target: None,
        )
        agent = SimpleNamespace(hil_read=leader_read)
        units.append(SimpleNamespace(name=name, robot=robot, agent=agent))

    io = StationIO(units)
    try:
        started = time.monotonic()
        q, leaders, buttons, ages = io.read()
        elapsed = time.monotonic() - started
    finally:
        io.close()

    # Serial execution takes at least 4 * delay. Leave ample CI scheduling margin
    # while still proving the four sleeps overlap.
    assert elapsed < delay * 3
    np.testing.assert_array_equal(q, np.r_[np.r_[np.ones(6), 0.2], np.r_[np.full(6, 2.0), 0.4]])
    np.testing.assert_array_equal(
        leaders, np.r_[np.r_[np.full(6, 3.0), 0.3], np.r_[np.full(6, 4.0), 0.6]]
    )
    assert buttons == [[True, False], [False, True]]
    assert ages == [0.001, 0.003, 1.001, 1.003]
    assert set(io.read_timings_s) == {
        "left_follower",
        "right_follower",
        "left_leader",
        "right_leader",
        "parallel_total",
    }


def test_verified_hardware_config_passes_and_placeholder_is_still_rejected():
    from yam_abc_reproduce.config import build_station_config

    cfg = build_station_config("configs/station_hil.yaml")
    validate_station(cfg, mock=False)
    cfg.robot.robots[0].gripper = "REPLACE_WITH_LINEAR_MOTOR_TYPE"
    with pytest.raises(ValueError, match="gripper"):
        validate_station(cfg, mock=False)


def test_dashboard_events_only_enqueue():
    from fastapi.testclient import TestClient

    from yam_abc_reproduce.hil.web import create_app

    called = []
    r = SimpleNamespace(status={"phase": "hold"}, event=called.append)
    with TestClient(create_app(r)) as client:
        assert client.get("/status").json()["phase"] == "hold"
        assert client.post("/event/takeover", headers={"X-YAM-Control": "1"}).status_code == 200
        assert called == ["takeover"]


def test_expert_export_splits_at_policy_gaps_and_preserves_video(tmp_path):
    from yam_abc_reproduce.data.formats.default_format import DefaultFormat
    from yam_abc_reproduce.hil.export import export

    im = np.zeros((32, 32, 3), np.uint8)
    rec = Recorder(tmp_path / "source", metadata={"mock": True, "station": {"task_name": "test"}})
    for i, source in enumerate(("human", "human", "policy", "human")):
        assert rec.submit(
            {
                "tick": i,
                "epoch": 1 if i < 2 else 2,
                "source": source,
                "expert_valid": source == "human",
                "observation_state": np.zeros(14),
                "submitted_action": np.zeros(14),
                "sync": {
                    "cameras": {r: {"host_received_at": i / 30} for r in ("top", "left", "right")}
                },
            },
            {r: im for r in ("top", "left", "right")},
        )
    rec.close("success")
    assert export(rec.path, tmp_path / "export") == 2
    for i, count in enumerate((2, 1)):
        meta, arrays = DefaultFormat().read_episode(tmp_path / "export" / f"episode_{i:06d}")
        assert meta.num_frames == count
        assert len(arrays["top-images-rgb"]) == count
        assert arrays["action-left-joint"].shape == (count, 6)


def test_freeze_targets_each_leader_current_pose_before_gravity_handover():
    q, h = np.zeros(14), np.zeros(14)
    h[[0, 7]] = 0.1
    calls = []
    units = []
    for name in ("left", "right"):
        robot = SimpleNamespace(
            joint_limits=lambda: np.tile([-3.0, 3.0], (6, 1)),
            get_joint_pos=lambda: np.zeros(7),
            command_joint_pos=lambda target: calls.append(("follower", target.copy())),
        )
        agent = SimpleNamespace(
            hil_leader_command=lambda target, **kw: calls.append(("leader", target.copy(), kw))
        )
        units.append(SimpleNamespace(name=name, robot=robot, agent=agent))
    io = StationIO(units)
    a = Arbiter(Mode.HIL)
    a.start(q)
    a.takeover(q, h)
    decision = a.step(q, h, now=0, dt=1 / 30)
    io.apply(decision, q, h, dt=1 / 30)
    for call in calls[:2]:
        assert call[0] == "leader" and not call[2]["manual"]
        assert call[1][0] == 0.1
    assert all(not np.any(call[1]) for call in calls[2:])


def test_runtime_keyboard_priority_and_handle_only_hands_back(tmp_path):
    import time

    cfg = StationConfig()
    io = StationIO(build_arm_units(cfg, mock=True), mock=True)
    keys = [[False, False], [False, False]]
    block, entered, release = threading.Event(), threading.Event(), threading.Event()
    real_read = io.read

    def read():
        if block.is_set():
            entered.set()
            assert release.wait(1)
        q, h, _, ages = real_read()
        return q, h, [p.copy() for p in keys], ages

    io.read = read
    cameras = [
        CameraWorker(MockCamera(r, r, width=32, height=32)) for r in ("top", "left", "right")
    ]
    rec = Recorder(tmp_path / "episode")
    worker = PolicyWorker(MockPolicy())
    runtime = Runtime(io, cameras, worker, rec)
    thread = threading.Thread(target=runtime.run, kwargs={"duration": 5, "auto_start": True})

    def wait(test):
        end = time.monotonic() + 2
        while not test() and time.monotonic() < end:
            time.sleep(0.005)
        assert test(), runtime.status

    try:
        for c in cameras:
            c.start()
        thread.start()
        wait(lambda: runtime.status["phase"] == "policy")
        block.set()
        assert entered.wait(1)
        for _ in range(16):
            runtime.event("start")
        runtime.event("takeover")  # succeeds even with full ordinary event queue
        release.set()
        wait(lambda: runtime.status["phase"] == "human")
        assert runtime.events.empty()
        runtime.event("takeover")  # repeated i cannot resume
        tick = runtime.status["tick"]
        wait(lambda: runtime.status["tick"] > tick + 2)
        assert runtime.status["phase"] == "human"
        keys[1][0] = True
        wait(lambda: runtime.status["phase"] == "policy")
        keys[1][1] = True
        tick = runtime.status["tick"]
        wait(lambda: runtime.status["tick"] > tick + 2)
        assert runtime.status["phase"] == "policy"  # second handle button is unassigned in HIL
        runtime.event("quit")
        thread.join(2)
        rec.close()
        rows = list(read_rows(rec.path))
        freeze = next(r for r in rows if "takeover_applied" in r["transitions"])
        assert freeze["source"] == "hold"
        assert freeze["event_applied_at"] >= freeze["event_requested_at"]
        following = next(r for r in rows if r["tick"] == freeze["tick"] + 1)
        assert following["source"] == "human" and following["expert_valid"]
        assert "human_started" in following["transitions"]
        assert any("resume_requested" in r["transitions"] for r in rows)
    finally:
        release.set()
        runtime.event("quit")
        thread.join(2)
        if rec._thread.is_alive():
            rec.close("aborted")
        for c in cameras:
            c.stop()
        worker.close()
        io.close()
