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
from yam_abc_reproduce.hil.recording import Recorder, RecordingSession
from yam_abc_reproduce.hil.run import MockPolicy, Runtime, validate_station
from yam_abc_reproduce.hil.session import Session
from yam_abc_reproduce.hil.station import StationIO
from yam_abc_reproduce.hil.storage import read_rows
from yam_abc_reproduce.runtime import build_arm_units


class DelayedRtcPolicy(MockPolicy):
    def infer(self, obs):
        time.sleep(0.219)
        return {"actions": np.tile(obs["observation.state"], (50, 1))}


def test_leader_home_uses_native_gain_without_changing_manual_or_hold():
    io = StationIO(build_arm_units(StationConfig(), mock=True), mock=True)
    calls = []
    for unit in io.units:
        unit.agent = SimpleNamespace(hil_leader_command=lambda target, **kw: calls.append(kw))
    io.mock = False  # Exercise real command selection with mock motor endpoints.
    q = np.zeros(14)
    a = Arbiter(Mode.HIL)
    d = a.step(q, q, now=0, dt=1/30, leader_ready=True)
    try:
        io.apply(d, q, q, dt=1/30, maintenance_leader=q, leader_homing=True)
        assert all(c == {"manual": False, "gain_scale": 1.0} for c in calls)
        calls.clear()
        d.leader_freeze = True
        io.apply(d, q, q, dt=1/30)
        assert all(c == {"manual": False, "gain_scale": .4} for c in calls)
        calls.clear()
        d.leader_freeze, d.leader_manual = False, True
        io.apply(d, q, q, dt=1/30)
        assert all(c["manual"] for c in calls)
        calls.clear()
        d.phase, d.leader_manual = Phase.RESUME, False
        io.apply(d, q, q, dt=1/30)
        assert all(c == {"manual": False, "gain_scale": 1.0} for c in calls)
    finally:
        io.mock = True
        io.close()


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


@pytest.mark.parametrize("fusion", ["raw", "rtc"])
def test_runtime_end_to_end_takeover_resume_and_recording(tmp_path, fusion):
    cfg = StationConfig()
    io = StationIO(build_arm_units(cfg, mock=True), mock=True,
                   policy_trajectory_hz=0 if fusion == "rtc" else 100)
    cameras = [
        CameraWorker(MockCamera(role, role, width=32, height=32))
        for role in ("top", "left", "right")
    ]
    rec = (RecordingSession(tmp_path / "session", mode="hil",
                            metadata={"rtc": False, "policy_fusion": "tda_smooth"})
           if fusion == "rtc" else Recorder(tmp_path / "episode"))
    worker = PolicyWorker(MockPolicy())
    try:
        for c in cameras:
            c.start()
        run = Runtime(io, cameras, worker, rec, settings={"policy_fusion": fusion})
        run.event("success")
        assert run.outcome == "unknown"  # consumed in the control owner
        result = run.run(duration=3.5, auto_start=True, demo=True)
        rec.close(run.outcome)
        assert not result["error"] and result["phase"] == "policy"
        path = rec.path / "episode_000001" if fusion == "rtc" else rec.path
        rows = list(read_rows(path))
        if fusion == "rtc":
            manifest = json.loads((path / "manifest.json").read_text())
            assert manifest["rtc"] and manifest["policy_fusion"] == "rtc"
            assert manifest["rtc_delay_steps"] == 9
            assert manifest["collection_mode"] == "hil"
            assert len(list(rec.path.glob("episode_*"))) == 1
            transitions = [t for row in rows for t in row["transitions"]]
            assert transitions == ["policy_started", "human_started",
                                   "resume_requested", "policy_started"]
        assert {"policy", "human", "hold"} <= {r["source"] for r in rows}
        human = [r for r in rows if r["source"] == "human"]
        assert all(not r["policy_valid"] and r["policy_action"] is None for r in human)
        assert any(r["expert_valid"] for r in human)
        epochs = [r["epoch"] for r in rows]
        assert epochs == sorted(epochs)
        assert any(r.get("policy_reply") for r in rows)
        policy_rows = [r for r in rows if r["source"] == "policy"]
        if fusion == "rtc":
            assert all(r["action_index"] == r["tick"] for r in policy_rows)
        else:
            assert any(r["policy_selection"] for r in policy_rows)
        traces = [sample for r in rows for sample in (r.get("policy_write_trace") or {}).get("samples", [])]
        assert bool(traces) == (fusion != "rtc")
        assert all(sample["arms"]["left"]["sdk_call_started_at"] <=
                   sample["arms"]["left"]["sdk_call_returned_at"] <=
                   sample["arms"]["right"]["sdk_call_started_at"] <=
                   sample["arms"]["right"]["sdk_call_returned_at"] for sample in traces)
        assert all(r["bounded_action"] is not None and r["bounded_at"] <= r["apply_returned_at"]
                   for r in policy_rows)
    finally:
        for c in cameras:
            c.stop()
        worker.close()
        io.close()
        if rec._thread.is_alive():
            rec.close("aborted")


@pytest.mark.parametrize("fusion", ("tda_smooth", "sync_hold"))
def test_policy_settings_change_only_in_hold_and_invalidate_old_reply(tmp_path, fusion):
    cfg = StationConfig()
    io = StationIO(build_arm_units(cfg, mock=True), mock=True)
    recorder = Recorder(tmp_path / "policy-settings")
    worker = PolicyWorker(MockPolicy())
    cameras = [CameraWorker(MockCamera(role, role, width=32, height=32))
               for role in ("top", "left", "right")]
    runtime = Runtime(io, cameras, worker, recorder, mode="inference", settings={"policy_fusion": "raw"})
    try:
        for camera in cameras:
            camera.start()
        old_epoch = runtime.session.arbiter.epoch
        runtime.configure_policy(fusion=fusion)
        with pytest.raises(ValueError, match="同步推理、TDA 或 RTC"):
            runtime.configure_policy(fusion="smooth")
        result = runtime.run(duration=0.15)
        assert result["policy_fusion"] == fusion
        assert result["policy_tda_drop_max"] == (25 if fusion == "tda_smooth" else None)
        assert result["policy_trajectory_mode"] == "second_order"
        assert runtime.session.arbiter.epoch > old_epoch
        runtime.status["phase"] = "policy"
        with pytest.raises(ValueError, match="暂停"):
            runtime.configure_policy(
                fusion="sync_hold" if fusion == "tda_smooth" else "tda_smooth"
            )
    finally:
        for camera in cameras:
            camera.stop()
        worker.close()
        io.close()
        recorder.close("aborted")


def test_mock_rtc_executes_only_post_commit_suffix_at_30hz(tmp_path):
    cfg = StationConfig()
    io = StationIO(build_arm_units(cfg, mock=True), mock=True)
    recorder = Recorder(tmp_path / "rtc")
    worker = PolicyWorker(MockPolicy())
    cameras = [CameraWorker(MockCamera(role, role, width=32, height=32))
               for role in ("top", "left", "right")]
    runtime = Runtime(
        io, cameras, worker, recorder, mode="inference",
        settings={"policy_fusion": "rtc", "replan_period": 0.2},
    )
    try:
        for camera in cameras:
            camera.start()
        result = runtime.run(duration=1.1, auto_start=True)
        assert result["policy_fusion"] == "rtc"
        assert result["rtc_delay_steps"] == 9
        recorder.close("aborted")
        rows = list(read_rows(recorder.path))
        replies = [row["policy_reply"] for row in rows if row.get("policy_reply")]
        assert any(not reply["discarded"] for reply in replies)
        policy = [row for row in rows if row.get("policy_valid")]
        assert policy
        assert result["policy_trajectory_hz"] == 0
        assert not result.get("error")
    finally:
        for camera in cameras:
            camera.stop()
        worker.close()
        io.close()
        if recorder._thread.is_alive():
            recorder.close("aborted")


def test_rtc_prefix_delay_changes_in_hold_without_restarting_device(tmp_path):
    cfg = StationConfig()
    io = StationIO(build_arm_units(cfg, mock=True), mock=True)
    recorder = Recorder(tmp_path / "rtc-delay-setting")
    worker = PolicyWorker(MockPolicy())
    cameras = [CameraWorker(MockCamera(role, role, width=32, height=32))
               for role in ("top", "left", "right")]
    runtime = Runtime(
        io, cameras, worker, recorder, mode="inference",
        settings={"policy_fusion": "rtc", "rtc_delay_steps": 9},
    )
    try:
        for camera in cameras:
            camera.start()
        runtime.configure_policy(fusion="rtc", rtc_delay_steps=10)
        result = runtime.run(duration=0.15)
        assert result["phase"] == "hold"
        assert result["rtc_delay_steps"] == 10
        assert runtime.session.arbiter.rtc_timeline.delay_steps == 10
        assert not result.get("error")
    finally:
        for camera in cameras:
            camera.stop()
        worker.close()
        io.close()
        recorder.close("aborted")


@pytest.mark.parametrize("delay_steps", (8, 9))
def test_rtc_219ms_rpc_does_not_block_control_loop(tmp_path, delay_steps):
    cfg = StationConfig()
    io = StationIO(build_arm_units(cfg, mock=True), mock=True)
    recorder = Recorder(tmp_path / f"rtc-delay-{delay_steps}")
    worker = PolicyWorker(DelayedRtcPolicy())
    cameras = [CameraWorker(MockCamera(role, role, width=32, height=32))
               for role in ("top", "left", "right")]
    runtime = Runtime(
        io, cameras, worker, recorder, mode="inference",
        settings={"policy_fusion": "rtc", "replan_period": 0.2},
    )
    runtime.session.arbiter.rtc_timeline.delay_steps = delay_steps
    try:
        for camera in cameras:
            camera.start()
        result = runtime.run(duration=2.5, auto_start=True)
        recorder.close("aborted")
        rows = list(read_rows(recorder.path))
        replies = [row["policy_reply"] for row in rows if row.get("policy_reply")]
        print("RTC delay", delay_steps, "replies", len(replies), "accepted",
              sum(not reply["discarded"] for reply in replies),
              "policy ticks", sum(bool(row.get("policy_valid")) for row in rows))
        assert result["deadline_misses"] == 0
        assert not result.get("error")
    finally:
        for camera in cameras:
            camera.stop()
        worker.close()
        io.close()
        if recorder._thread.is_alive():
            recorder.close("aborted")


def test_policy_trajectory_has_one_writer_and_stops_before_hold_direct_io():
    cfg = StationConfig()
    io = StationIO(
        build_arm_units(cfg, mock=True),
        mock=True,
        policy_trajectory_hz=100,
        policy_joint_speed=3,
        policy_joint_acceleration=30,
        policy_natural_frequency=10,
    )
    q = np.zeros(14)
    leader = np.zeros(14)
    policy = SimpleNamespace(
        action=np.r_[np.ones(6), 0.5, np.ones(6), 0.5],
        source="policy",
        leader_manual=True,
        leader_freeze=False,
    )
    hold = SimpleNamespace(
        action=q.copy(),
        source="hold",
        leader_manual=True,
        leader_freeze=False,
    )
    try:
        submitted, _ = io.apply(policy, q, leader, dt=1 / 30)
        assert submitted[0] == pytest.approx(0)
        deadline = time.monotonic() + 0.5
        while io._policy_trajectory.latest()[0] <= 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert io._policy_trajectory.latest()[0] > 0

        submitted, _ = io.apply(hold, q, leader, dt=1 / 30)
        assert io._policy_trajectory is None
        np.testing.assert_array_equal(submitted, q)
        np.testing.assert_array_equal(
            np.concatenate([unit.robot.get_joint_pos() for unit in io.units]), q
        )
    finally:
        io.close()


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
    leader.set_manual_control(False, 1.0)
    np.testing.assert_array_equal(device.calls[-2][1], np.ones(6) * 10)
    np.testing.assert_array_equal(device.calls[-2][2], np.ones(6) * 2)
    leader.set_manual_control(False, .4)
    np.testing.assert_array_equal(device.calls[-2][1], np.ones(6) * 4)
    leader.set_manual_control(True)
    assert device.calls[-1][0] == "gravity"


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


def test_aligns_each_leader_before_gravity_handover():
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
        assert 0 < call[1][0] < 0.1
        assert call[2]["gain_scale"] == 1.0
    assert all(not np.any(call[1]) for call in calls[2:])
    calls.clear()
    io.apply(a.step(q, h + .02, now=.1, dt=1/30), q, h + .02, dt=1/30)
    assert all(0 < call[1][0] < .1 for call in calls[:2])
    for i in range(30):
        h = a._leader_frozen.copy()
        a.step(q, h, now=.2+i/30, dt=1/30)
    a.manual_ready(q, h)
    calls.clear()
    io.apply(a.step(q, h, now=.2, dt=1/30), q, h, dt=1/30)
    assert all(call[2]["manual"] and call[2]["gain_scale"] == .2 for call in calls[:2])
    io.close()


@pytest.mark.parametrize("mirror", [True, False])
def test_policy_leaders_share_submitted_joint_targets_only_in_hil(mirror):
    calls = []
    units = []
    for name in ("left", "right"):
        robot = SimpleNamespace(
            joint_limits=lambda: np.tile([-1., 1.], (6, 1)),
            get_joint_pos=lambda: np.zeros(7),
            command_joint_pos=lambda target: calls.append(("follower", target.copy())),
        )
        agent = SimpleNamespace(
            hil_leader_command=lambda target, **kw: calls.append(("leader", target.copy(), kw))
        )
        units.append(SimpleNamespace(name=name, robot=robot, agent=agent))
    io = StationIO(units)
    d = SimpleNamespace(action=np.r_[[2., .4, .3, -.4, -.5, -.6, .7],
                                    [-2., -.4, -.3, .4, .5, .6, .2]],
                        source="policy", leader_manual=False, leader_freeze=False)
    try:
        submitted, _ = io.apply(d, np.zeros(14), np.zeros(14), dt=1 / 30, mirror=mirror)
        for i, (_, target, kw) in enumerate(calls[:2]):
            assert kw["manual"] is (not mirror)
            assert kw["gain_scale"] == (1.0 if mirror else .2)
            assert target.shape == (6,)
            np.testing.assert_array_equal(target, submitted[i * 7:i * 7 + 6])
        np.testing.assert_array_equal(submitted, np.concatenate([c[1] for c in calls[2:]]))
        assert submitted[0] == 1 and submitted[7] == -1
    finally:
        io.close()


def test_filtered_policy_leader_uses_sampled_target_not_raw_model(monkeypatch):
    from yam_abc_reproduce.hil import station

    class FakeTrajectory:
        def __init__(self, *args, **kwargs):
            pass

        def submit(self, target):
            self.target = target.copy() * .1

        def latest(self):
            return self.target.copy()

        def drain_trace(self):
            return None

        def close(self):
            pass

    monkeypatch.setattr(station, "TrajectoryExecutor", FakeTrajectory)
    io = StationIO(build_arm_units(StationConfig(), mock=True), mock=True,
                   policy_trajectory_hz=100)
    d = SimpleNamespace(action=np.full(14, .8), source="policy",
                        leader_manual=False, leader_freeze=False)
    try:
        submitted, _ = io.apply(d, np.zeros(14), np.zeros(14), dt=1 / 30)
        for start in (0, 7):
            np.testing.assert_allclose(io._mock_leaders[start:start + 6],
                                       submitted[start:start + 6])
        np.testing.assert_allclose(submitted, .08)
    finally:
        io.close()


def test_panel_stop_overrides_mirror_and_holds_both_leaders_and_followers():
    from yam_abc_reproduce.hil.maintenance import Maintenance

    q, h = np.full(14, .2), np.full(14, .6)
    calls = []
    units = []
    for name in ("left", "right"):
        robot = SimpleNamespace(
            joint_limits=lambda: np.tile([-3., 3.], (6, 1)),
            get_joint_pos=lambda: np.zeros(7),
            command_joint_pos=lambda target: calls.append(("follower", target.copy())),
        )
        agent = SimpleNamespace(
            hil_leader_command=lambda target, **kw: calls.append(("leader", target.copy(), kw))
        )
        units.append(SimpleNamespace(name=name, robot=robot, agent=agent))
    io, m, a = StationIO(units), Maintenance(), Arbiter(Mode.HIL, policy_fusion="rtc")
    a.start(q)
    event = m.command("stop", q, h, now=0, paused=False)
    a.hold(q)
    assert event == "hold" and m.latched
    # Feedback moves after the stop. Targets must stay at the stop snapshot,
    # not continue mirroring the follower or enter gravity-only control.
    for tick in range(1, 4):
        q_now, h_now = q + .01, h + .001
        d = a.step(q_now, h_now, now=tick / 30, dt=1 / 30, policy_tick=tick)
        target, leader_target = m.step(q_now, h_now, now=tick / 30, dt=1 / 30)
        d.action = target
        calls.clear()
        io.apply(d, q_now, h_now, dt=1 / 30, mirror=True,
                 maintenance_leader=leader_target)
        for kind, target, kw in calls[:2]:
            assert kind == "leader" and kw["manual"] is False
            np.testing.assert_allclose(target, .6)
        for kind, target in calls[2:]:
            assert kind == "follower"
            np.testing.assert_allclose(target, .2)


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
        wait(lambda: runtime.status["phase"] == "takeover")
        keys[1][0] = True
        wait(lambda: runtime.status["phase"] == "human")
        keys[1][0] = False
        time.sleep(.3)
        assert runtime.events.empty()
        runtime.event("takeover")  # repeated i cannot resume
        tick = runtime.status["tick"]
        wait(lambda: runtime.status["tick"] > tick + 2)
        assert runtime.status["phase"] == "human"
        keys[1][0] = True
        wait(lambda: runtime.status["phase"] == "hold")
        assert runtime.status["leader_locked"]
        runtime.event("resume_policy")
        wait(lambda: runtime.status["phase"] == "policy")
        keys[1][1] = True
        tick = runtime.status["tick"]
        wait(lambda: runtime.status["tick"] > tick + 2)
        assert runtime.status["phase"] == "policy"  # second handle button is unassigned in HIL
        runtime.event("quit")
        thread.join(2)
        rec.close()
        rows = list(read_rows(rec.path))
        assert not any(r["phase"] == "takeover" for r in rows)
        human = next(r for r in rows if "human_started" in r["transitions"])
        gap = human["omitted_intervention_wait"]
        assert gap["event_applied_at"] >= gap["event_requested_at"]
        assert gap["last_tick"] < human["tick"]
        assert any("human_started" in r["transitions"] for r in rows)
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
