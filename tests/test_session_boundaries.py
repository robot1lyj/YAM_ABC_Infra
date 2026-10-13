"""Fault injection for HOLD, storage publication and complete resource cleanup.

No motor, CAN, physical camera, running Web service or external policy access.
"""

import json
import threading
import time
from contextlib import nullcontext
from types import SimpleNamespace as NS

import numpy as np
import pytest

from yam_abc_reproduce.hil import recording, run, storage
from yam_abc_reproduce.hil.recording_service import RemoteRecordingSession
from yam_abc_reproduce.hil.station import StationIO


def wait_for(predicate):
    until = time.monotonic() + 3
    while not predicate() and time.monotonic() < until:
        time.sleep(.01)
    assert predicate()


@pytest.mark.parametrize("change,configure,reload", [
    ("idle", True, True), ("saving", False, True), ("intervention", False, True),
    ("recording", False, False), ("emergency", False, False), ("maintenance", False, False),
    ("switching", False, False), ("queued", False, False),
    ("accepted", True, True), ("rejected", True, True),
])
def test_policy_button_permissions_come_from_owner_rules(change, configure, reload):
    from yam_abc_reproduce.hil.core import Arbiter, Mode
    runtime = run.Runtime.__new__(run.Runtime)
    runtime.session = NS(arbiter=Arbiter(Mode.HIL))
    runtime.recorder = NS(recording=change == "recording", saving=change == "saving")
    runtime.maintenance = NS(latched=False, state="homing" if change == "maintenance" else "idle")
    runtime.task_switching = change == "switching"
    runtime.emergency = threading.Event()
    if change == "emergency":
        runtime.emergency.set()
    runtime.session.arbiter.intervention_pending = change == "intervention"
    runtime.policy_command_status = {"state": change} if change in ("queued", "accepted", "rejected") else None
    controls = runtime.policy_controls()
    assert controls["configure"]["allowed"] is configure
    assert controls["reload"]["allowed"] is reload
    assert bool(controls["configure"]["reason"]) is not configure
    assert bool(controls["reload"]["reason"]) is not reload


@pytest.mark.parametrize("initialize", [False, True])
def test_workbench_connects_without_recording_mount(tmp_path, monkeypatch, initialize):
    from yam_abc_reproduce.hil.workbench import Workbench
    missing = tmp_path / "unmounted"
    monkeypatch.setenv("YAM_RECORDING_MOUNT", str(missing))
    service = Workbench(NS(mode="hil", mock=True, url=None, station="configs/station_hil.yaml",
                           output=missing / "episodes", task_root=tmp_path / "tasks"))
    service.preview_enabled = False
    try:
        service.connect(initialize=initialize)
        deadline = time.monotonic() + 15
        while service.status.get("tick", 0) < 3 and time.monotonic() < deadline:
            service.heartbeat()
            time.sleep(.05)
        assert service.state == "connected" and service.status["phase"] == "hold", service.status
        assert not service.runtime.recording_allowed and not service.runtime.recorder.error
        assert not missing.exists()
    finally:
        service.close()
    assert service.cleanup_error is None and not missing.exists()


def fake_station(*, stale=None, fail_close=None):
    commands, closed, units = {}, [], []
    for index, name in enumerate(("left", "right")):
        q = np.full(7, index * .1)
        h = q + .2
        def release(role):
            closed.append(role)
            if fail_close == role:
                raise RuntimeError("injected SDK close failure")
        robot = NS(
            get_joint_pos=lambda q=q: q.copy(),
            hil_read=lambda q=q, n=name: (q.copy(), 1.0 if stale == n + " follower" else .001),
            joint_limits=lambda: np.tile([-3., 3.], (6, 1)),
            command_joint_pos=lambda q, n=name: commands.update({n + " follower": q.copy()}),
            close_hil=lambda n=name: release(n + " follower"),
        )
        agent = NS(
            hil_read=lambda h=h, n=name: (h.copy(), [], 1.0 if stale == n + " leader" else .002),
            hil_leader_command=lambda q, *, manual, gain_scale, n=name:
                commands.update({n + " leader": (q.copy(), manual, gain_scale)}),
            close_hil=lambda n=name: release(n + " leader"),
        )
        units.append(NS(name=name, robot=robot, agent=agent))
    return StationIO(units), commands, closed


@pytest.mark.parametrize("stale", [None, "left follower", "right leader"])
def test_fault_hold_locks_fresh_leaders_and_skips_stale_targets(stale):
    io, commands, _ = fake_station(stale=stale)
    try:
        errors = io.hold()
        assert bool(errors) == bool(stale)
        if stale:
            assert stale in errors[0] and "反馈过期" in errors[0]
            assert stale not in commands
        for side, position in (("left", .2), ("right", .3)):
            if side + " leader" != stale:
                target, manual, gain = commands[side + " leader"]
                np.testing.assert_allclose(target, position)
                assert manual is False and gain == .4
        assert len(commands) == (3 if stale else 4)
    finally:
        assert not io.close()  # Staleness must not block explicit SDK release.


def test_sdk_release_attempts_all_devices_after_one_close_failure():
    io, _, closed = fake_station(fail_close="left leader")
    errors = io.close()
    assert closed == ["left leader", "left follower", "right leader", "right follower"]
    assert len(errors) == 1 and "left leader" in errors[0]


def test_recording_fault_keeps_leaders_frozen_on_following_ticks():
    from yam_abc_reproduce.hil.core import Arbiter, Mode
    runtime = run.Runtime.__new__(run.Runtime)
    runtime.recording_error = None
    runtime.recorder = NS(metadata={}, abort_episode=lambda: None)
    runtime.io = NS(hold=lambda: [])
    runtime.session = NS(arbiter=Arbiter(Mode.HIL))
    q, leader = np.zeros(14), np.full(14, .2)
    runtime._recording_failed(q, leader, "disk unavailable")
    decision = runtime.session.arbiter.step(q, leader, now=1, dt=1/30)
    assert decision.leader_freeze and not decision.leader_manual
    np.testing.assert_array_equal(decision.leader_hold_target, leader)


def test_live_trajectory_prevents_a_second_writer_or_sdk_release():
    io, commands, closed = fake_station()
    io._policy_trajectory = NS(alive=True, close=lambda: (_ for _ in ()).throw(
        RuntimeError("trajectory executor did not stop")))
    try:
        assert io.hold() and io.close()
        assert not commands and not closed
    finally:
        io._policy_trajectory = None
        io.close()


@pytest.mark.parametrize("factory", [recording.RecordingSession, RemoteRecordingSession])
def test_unrecorded_session_ignores_missing_data_mount(tmp_path, monkeypatch, factory):
    monkeypatch.setenv("YAM_RECORDING_MOUNT", str(tmp_path / "missing-disk"))
    path = tmp_path / "missing-disk" / "teleop"
    recorder = factory(path, mode="teleop", defer_storage=True)
    try:
        assert recorder.submit({}, {})
        assert not recorder.error and not path.exists()
        with pytest.raises((OSError, RuntimeError), match="not mounted"):
            recorder.bind_task(path.parent / "recorded", {"task": "pick"})
        assert not recorder.error and recorder.path == path
    finally:
        recorder.close()
    assert not path.exists()


def test_deferred_binding_failure_can_retry_without_changing_session(tmp_path, monkeypatch):
    rec = recording.RecordingSession(tmp_path / "teleop", mode="teleop", defer_storage=True)
    target = tmp_path / "recorded"
    try:
        with monkeypatch.context() as failure:
            failure.setattr(storage, "atomic_json", lambda *a: (_ for _ in ()).throw(OSError("disk write")))
            with pytest.raises(OSError, match="disk write"):
                rec.bind_task(target, {"task": "pick"})
        assert not target.exists() and not rec.path.exists() and not rec.error
        rec.bind_task(target, {"task": "pick"})
        assert rec.path == target and json.loads((target / "session.json").read_text())["task"] == "pick"
    finally:
        rec.close()


class FinishedEpisode:
    def __init__(self, path, **kw):
        self.path, self.metadata = path, kw["metadata"]
        self.written, self.error = 12, None
    def close(self, outcome):
        pass


@pytest.mark.parametrize("persistent", [False, True])
def test_failed_manifest_never_publishes_a_successful_episode(tmp_path, monkeypatch, persistent):
    monkeypatch.setattr(recording, "Recorder", FinishedEpisode)
    rec = recording.RecordingSession(tmp_path / "recorded")
    original = storage.atomic_json
    calls = []
    def fail(path, value):
        calls.append(value)
        if persistent or len(calls) == 1:
            raise OSError("injected manifest I/O failure")
        original(path, value)
    monkeypatch.setattr(storage, "atomic_json", fail)
    try:
        rec.start_episode()
        rec.stop_episode("success")
        wait_for(lambda: not rec._thread.is_alive())
        assert "manifest I/O failure" in rec.error
        disk = json.loads((rec.path / "session.json").read_text())
        assert rec.episodes == disk["episodes"]
        assert all(e["outcome"] == "aborted" for e in rec.episodes)
        assert rec._completed_steps == (0 if persistent else 12)
    finally:
        rec.close()


@pytest.mark.parametrize("run_fails", [False, True])
@pytest.mark.parametrize("transport_alive", [False, True])
def test_session_cleanup_continues_and_preserves_primary_error(tmp_path, monkeypatch, run_fails, transport_alive):
    calls = []
    class Recorder:
        def __init__(self, *args, **kwargs):
            self.metadata, self.error = {}, None
            self._thread = NS(is_alive=lambda: transport_alive)
        def close(self, outcome):
            calls.append("recording")
    class IO:
        def __init__(self, *args, **kwargs): pass
        def close(self):
            calls.append("arms")
            raise RuntimeError("SDK cleanup failed")
    class Worker:
        def __init__(self, *args, **kwargs): pass
        def close(self): calls.append("policy")
    class CameraWorker:
        def __init__(self, camera): self.camera = camera
        def start(self): pass
        def stop(self):
            calls.append(self.camera)
            raise RuntimeError("camera cleanup failed")
    class Runtime:
        def __init__(self, io, cameras, worker, recorder, **kwargs):
            self.recorder, self.outcome = recorder, "unknown"
        def event(self, event): pass
        def run(self, **kwargs):
            if run_fails:
                raise RuntimeError("original control fault")
            return {"phase": "hold"}
    for name, value in dict(RecordingSession=Recorder, StationIO=IO, PolicyWorker=Worker,
        CameraWorker=CameraWorker, Runtime=Runtime, build_arm_units=lambda *a, **k: [],
        build_cameras_from_config=lambda *a, **k: ["camera1", "camera2"],
        place_on_cpus=lambda *a: None).items():
        monkeypatch.setattr(run, name, value)
    from yam_abc_reproduce.hil import keyboard
    monkeypatch.setattr(keyboard, "Keyboard", lambda *a: nullcontext())
    with pytest.raises(RuntimeError, match="original control fault" if run_fails else "Session cleanup"):
        run.main(["--mock", "--mode", "teleop", "--output", str(tmp_path)])
    assert calls == ["arms", "camera1", "camera2", "policy", "recording"]


def test_policy_cleanup_does_not_skip_planner_after_transport_close_error():
    from yam_abc_reproduce.hil.policy import PolicyWorker
    closed = []
    def fail():
        closed.append("transport")
        raise OSError("transport close failed")
    # RTC avoids starting an unused planner; both resources must still close.
    worker = PolicyWorker(NS(rtc=True, close=fail),
                          planner=NS(alive=True, close=lambda: closed.append("planner")))
    with pytest.raises(RuntimeError, match="transport close failed"):
        worker.close()
    assert not worker._thread.is_alive() and closed == ["transport", "planner"]


def test_complete_control_work_includes_statistics_overhead(monkeypatch):
    from yam_abc_reproduce.hil import metrics
    latency = metrics.Latencies()
    clock = [10.0]
    original = metrics.np.percentile
    def expensive_summary(*args):
        clock[0] += .002
        return original(*args)
    monkeypatch.setattr(metrics, "monotonic", lambda: clock[0])
    monkeypatch.setattr(metrics.np, "percentile", expensive_summary)
    clock[0] += .003  # Application + status work before reporting.
    latency.finish_tick(10.0, io_apply=.001)
    assert latency.values["control_work"][-1] == pytest.approx(5.0)
    clock[0] = 11.004
    report = latency.finish_tick(11.0, io_apply=.001)
    assert report["control_work"]["p95_ms"] == pytest.approx(5.0)
    assert latency.values["control_work"][-1] == pytest.approx(8.0)


def test_direct_sdk_timestamps_are_per_arm_and_do_not_change_targets(monkeypatch):
    from yam_abc_reproduce.hil import station
    io, commands, _ = fake_station()
    clock = [20.0]
    monkeypatch.setattr(station.time, "monotonic", lambda: clock[0])
    for unit in io.units:
        def write(target, name=unit.name):
            commands[name + " follower"] = target.copy()
            clock[0] += .006 if name == "left" else .009
        unit.robot.command_joint_pos = write
    target = np.linspace(.1, .6, 14)
    decision = NS(action=target.copy(), source="policy", leader_manual=True, leader_freeze=False)
    try:
        submitted, stamps = io.apply(decision, np.zeros(14), np.zeros(14), dt=1/30, mirror=False)
        np.testing.assert_array_equal(submitted, target)
        np.testing.assert_array_equal(np.r_[commands["left follower"], commands["right follower"]], target)
        assert stamps["left_follower"] == pytest.approx(20.006)
        assert stamps["right_follower"] == pytest.approx(20.015)
        assert io.write_timings_s["left_follower"] == pytest.approx(.006)
        assert io.write_timings_s["right_follower"] == pytest.approx(.009)
    finally:
        io.close()


@pytest.mark.parametrize("old_time,valid", [(.99, True), (.4, False), (1.01, False)])
def test_camera_arrival_during_tick_uses_only_valid_earlier_frames(old_time, valid):
    from yam_abc_reproduce.hil.observation import Observations
    def frame(at, seq):
        return NS(images={"rgb": np.zeros((2, 2, 3), dtype=np.uint8)},
                  meta={"host_received_at": at, "sequence": seq})
    cameras = [NS(role=role, history=lambda: [frame(old_time, 1), frame(1.02, 2)])
               for role in ("top", "left", "right")]
    observations = Observations(cameras)
    observations.add_state(.3, np.zeros(14))
    observations.add_state(1., np.zeros(14))
    result = observations.snapshot(1., "test")
    assert (result is not None) is valid
    if valid:
        assert result[1] == .99
        assert all(meta["sequence"] == 1 for meta in result[4]["cameras"].values())
