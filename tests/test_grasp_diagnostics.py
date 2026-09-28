import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from yam_abc_reproduce.hil.grasp_diagnostics import Thresholds, analyze
from yam_abc_reproduce.hil.rtc_timeline import RtcTimeline
from yam_abc_reproduce.hil.storage import Samples, h5_rows
from yam_abc_reproduce.hil.web import create_app
from yam_abc_reproduce.robot.yam_adapter import YamRobot


def rows(*, empty=False):
    result = []
    for tick in range(12):
        action = np.zeros(14)
        action[[6, 13]] = .8 if tick < 2 else .1
        feedback = dict(position=.02 if empty else .25, velocity=0., effort_nm=.7,
                        sdk_updated_at=1000 + tick / 30, sampled_at=tick / 30,
                        feedback_age_s=.005, valid=True)
        result.append(dict(tick=tick, time=tick/30, epoch=1, source="policy",
                           submitted_action=action, frame_index=tick,
                           video_indices={"top": tick, "left": tick, "right": tick},
                           policy_selection={"model_index": tick, "request": {"request_id": 7}},
                           grasp_diagnostics={"schema_version": 1, "sample_phase": "before_command",
                                              "followers": [feedback, dict(feedback)]}))
    return result


def report(data):
    return analyze(data, left=Thresholds(.5, .02), right=Thresholds(.8, .02))


def test_contact_is_only_a_candidate_with_onset_confirmation_and_previous_command():
    result = report(rows())
    left, right = result["attempts"]
    assert left["command_start"]["tick"] == 2
    candidate = left["candidate"]
    assert candidate["kind"] == "contact_candidate"
    assert candidate["onset"]["tick"] == 3  # Not the pre-command sample at tick 2.
    assert candidate["confirmed"]["time"] - candidate["onset"]["time"] >= .1
    assert candidate["preceding_command"]["tick"] == 2
    assert candidate["onset"]["policy_selection"]["request"]["request_id"] == 7
    assert left["review_result"] is None
    assert right["candidate"] is None  # Separate per-arm thresholds; retained attempt.


def test_empty_endstop_is_not_contact_even_with_high_torque():
    assert all(a["candidate"]["kind"] == "empty_close_candidate"
               for a in report(rows(empty=True))["attempts"])


@pytest.mark.parametrize("bad", ["missing", "repeated", "stale", "fast", "nan"])
def test_missing_stale_repeated_or_moving_feedback_cannot_confirm(bad):
    data = rows()
    for row in data:
        feedback = row["grasp_diagnostics"]["followers"][0]
        if bad == "missing":
            feedback["valid"] = False
        elif bad == "repeated":
            feedback["sdk_updated_at"] = 1000
        elif bad == "stale":
            feedback["feedback_age_s"] = .3
        elif bad == "fast":
            feedback["velocity"] = .8
        else:
            feedback["effort_nm"] = float("nan")
    assert report(data)["attempts"][0]["candidate"] is None


def test_removed_wait_or_epoch_does_not_bridge_confirmation():
    data = rows()[:7]
    data[5]["wait_boundary"] = True
    result = report(data)
    assert result["attempts"][0]["candidate"] is None
    assert result["attempts"][0]["end_reason"] == "timeline_boundary"


def test_reopen_rearms_and_old_data_does_not_fabricate_force():
    data = rows()
    second = copy.deepcopy(data)
    for row in second:
        row["tick"] += 12
        row["time"] += .4
        row.pop("grasp_diagnostics")
    result = report(data + second)
    left = [a for a in result["attempts"] if a["arm"] == "left"]
    assert len(left) == 2
    assert left[0]["end_reason"] == "reopened"
    assert left[1]["candidate"] is None
    assert left[1]["missing_feedback_frames"] > 0
    assert result["diagnostic_frames"] == 12


def test_sdk_snapshot_is_coherent_and_does_not_query_hardware():
    robot = YamRobot.__new__(YamRobot)
    robot._n, robot._g_open, robot._g_closed = 6, 1., 0.
    sdk_state = SimpleNamespace(pos=np.arange(7)/10, vel=np.arange(7)/20,
                                eff=np.arange(7)/30, timestamp=time.time())
    robot._robot = SimpleNamespace(_server_thread=SimpleNamespace(is_alive=lambda: True),
                                   _joint_state=sdk_state)
    q, age, feedback = robot.hil_read_with_gripper()
    assert q[-1] == feedback["position"] == .6
    assert feedback["velocity"] == .3
    assert feedback["effort_nm"] == .2 and feedback["valid"]
    assert feedback["feedback_age_s"] == age
    q[0] = 999
    assert sdk_state.pos[0] == 0
    sdk_state.eff = None
    assert robot.hil_read_with_gripper()[2]["effort_nm"] is None
    assert not robot.hil_read_with_gripper()[2]["valid"]


def test_station_and_remote_executor_propagate_same_feedback_snapshot():
    from yam_abc_reproduce.config import StationConfig
    from yam_abc_reproduce.hil.executor_service import Executor
    from yam_abc_reproduce.hil.remote_station import RemoteStationIO
    from yam_abc_reproduce.hil.station import StationIO
    from yam_abc_reproduce.runtime import build_arm_units

    io = StationIO(build_arm_units(StationConfig(), mock=True), mock=True)
    feedback = rows()[0]["grasp_diagnostics"]["followers"]
    calls = []
    for i, unit in enumerate(io.units):
        def read(index=i):
            calls.append(index)
            return np.zeros(7), .005, feedback[index]
        unit.robot.hil_read_with_gripper = read
        unit.agent = SimpleNamespace(hil_read=lambda: (np.zeros(7), [False, False], .005))
    io.mock = False
    io._read_pool = ThreadPoolExecutor(max_workers=4)
    io.leader_control_status = lambda: {}
    owner = Executor(lambda: io)
    owner.io = io
    try:
        owner._read()
        assert sorted(calls) == [0, 1]  # One snapshot per follower, no second read.
        assert owner.snapshot["gripper_feedback"] == feedback
        proxy = RemoteStationIO.__new__(RemoteStationIO)
        proxy._rpc = lambda _: {"fault": None, "snapshot": owner.snapshot}
        proxy.read()
        assert proxy.gripper_feedback == feedback
        del owner.snapshot["gripper_feedback"]
        proxy.read()
        assert proxy.gripper_feedback == [None, None]  # Compatible older owner.
    finally:
        io.mock = True
        io.close()


def test_hdf5_round_trip_preserves_telemetry_and_provenance(tmp_path):
    path = tmp_path / "samples.h5"
    writer = Samples(path)
    data = rows()
    for row in data:
        writer.append(row)
    writer.close()
    restored = list(h5_rows(path))
    assert len(restored) == len(data)
    for original, saved in zip(data, restored, strict=True):
        assert saved["grasp_diagnostics"] == original["grasp_diagnostics"]
        assert saved["policy_selection"] == original["policy_selection"]
        np.testing.assert_array_equal(saved["submitted_action"], original["submitted_action"])
    assert report(restored)["attempts"][0]["candidate"]["kind"] == "contact_candidate"


def test_recording_settings_api_validates_and_only_forwards_recording():
    calls = []
    owner = SimpleNamespace(status={}, configure_recording=lambda **kw: calls.append(kw))
    with TestClient(create_app(owner)) as client:
        for mode in ("standard", "grasp_diagnostics"):
            response = client.post("/recording/settings", headers={"X-YAM-Control": "1"},
                                   json={"mode": mode})
            assert response.status_code == 200
        assert client.post("/recording/settings", headers={"X-YAM-Control": "1"},
                           json={"mode": "auto_close"}).status_code == 422
    assert calls == [{"mode": "standard"}, {"mode": "grasp_diagnostics"}]


def test_rtc_provenance_tracks_original_prefix_not_latest_request():
    clock = RtcTimeline(delay_steps=3)
    q = np.zeros(14)
    clock.record_submitted(0, q)
    first = clock.prepare(observation_tick=0, current_tick=0, limit_target=lambda v: v)
    actions = np.ones((50, 14)) * .1
    actions[:3] = first.actions
    assert clock.install(first, actions, current_tick=2, limit_target=lambda v: v,
                         request={"request_id": 1})
    for tick in range(1, 5):
        value, _ = clock.select(tick, q)
        clock.record_submitted(tick, value)
    second = clock.prepare(observation_tick=4, current_tick=4, limit_target=lambda v: v)
    actions2 = np.ones((50, 14)) * .2
    actions2[:3] = second.actions
    assert clock.install(second, actions2, current_tick=6, limit_target=lambda v: v,
                         request={"request_id": 2})
    value, _ = clock.select(6, q)
    np.testing.assert_allclose(value, .1)
    assert clock.last_selection["request"]["request_id"] == 1
    assert clock.last_selection["model_index"] == 6
    value, _ = clock.select(7, q)
    np.testing.assert_allclose(value, .2)
    assert clock.last_selection["request"]["request_id"] == 2
    assert clock.last_selection["model_index"] == 3
    json.dumps(clock.last_selection, allow_nan=False)
    clock.clear()
    clock.select(8, q)
    assert clock.last_selection is None


@pytest.mark.parametrize("mode", ["standard", "grasp_diagnostics"])
def test_runtime_recording_switch_does_not_reset_epoch_or_move_targets(tmp_path, mode):
    from yam_abc_reproduce.camera.mock_camera import MockCamera
    from yam_abc_reproduce.camera.worker import CameraWorker
    from yam_abc_reproduce.config import StationConfig
    from yam_abc_reproduce.hil.recording import Recorder
    from yam_abc_reproduce.hil.run import Runtime
    from yam_abc_reproduce.hil.station import StationIO
    from yam_abc_reproduce.hil.storage import read_rows
    from yam_abc_reproduce.runtime import build_arm_units

    io = StationIO(build_arm_units(StationConfig(), mock=True), mock=True)
    rec = Recorder(tmp_path / mode)
    cameras = [CameraWorker(MockCamera(role, role, width=32, height=32))
               for role in ("top", "left", "right")]
    runtime = Runtime(io, cameras, None, rec, mode="inference")
    try:
        for camera in cameras:
            camera.start()
        initial_epoch = runtime.session.arbiter.epoch
        runtime.configure_recording(mode=mode)
        status = runtime.run(duration=.15)
        assert status["phase"] == "hold", status
        assert status["recording_mode"] == mode
        assert status["policy_command"]["state"] == "accepted"
        assert runtime.session.arbiter.epoch == initial_epoch
        assert io.grasp_diagnostics_enabled == (mode == "grasp_diagnostics")
        rec.close("unknown")
        saved = list(read_rows(rec.path))
        assert saved
        assert all(("grasp_diagnostics" in row) == (mode == "grasp_diagnostics") for row in saved)
        assert all(np.count_nonzero(row["submitted_action"]) == 0 for row in saved)
        runtime.status["phase"] = "policy"
        with pytest.raises(ValueError, match="暂停"):
            runtime.configure_recording(mode=mode)
        runtime.status["phase"] = "hold"
        with pytest.raises(ValueError, match="记录模式"):
            runtime.configure_recording(mode="close_gripper")
    finally:
        for camera in cameras:
            camera.stop()
        io.close()
        rec.close("unknown")
