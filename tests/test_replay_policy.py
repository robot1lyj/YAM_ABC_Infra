import json
import time

import numpy as np
import pytest

from yam_abc_reproduce.hil.replay_policy import (
    ReplayPolicy, load_targets, load_xr1_eef_targets, retime_targets_for_step,
    time_stretch_targets,
)


def test_replay_time_stretch_keeps_endpoints_and_reduces_step_size():
    source = np.zeros((4, 14))
    source[:, 0] = [0, 0.12, 0.24, 0.36]
    source[:, 6] = [1, 1, 0, 0]
    slowed = time_stretch_targets(source, .75)
    assert len(slowed) == 5
    np.testing.assert_array_equal(slowed[0], source[0])
    np.testing.assert_array_equal(slowed[-1], source[-1])
    assert np.max(np.diff(slowed[:, 0])) == pytest.approx(.09)
    assert np.min(slowed[:, 6]) >= 0 and np.max(slowed[:, 6]) <= 1
    np.testing.assert_array_equal(time_stretch_targets(source), source)
    with pytest.raises(ValueError, match="playback rate"):
        time_stretch_targets(source, 1.1)


def test_replay_adaptive_retime_preserves_path_and_caps_arm_step():
    source = np.zeros((4, 14))
    source[:, 0] = [0, .01, .11, .12]
    source[:, 6] = [1, 1, 0, 0]
    output = retime_targets_for_step(source, .03)
    assert len(output) == 7
    np.testing.assert_array_equal(output[0], source[0])
    np.testing.assert_array_equal(output[-1], source[-1])
    assert np.max(np.abs(np.diff(output[:, 0]))) <= .03 + 1e-12
    assert output[1, 0] == pytest.approx(source[1, 0])
    assert np.min(output[:, 6]) >= 0 and np.max(output[:, 6]) <= 1
    with pytest.raises(ValueError, match="max arm replay step"):
        retime_targets_for_step(source, 0)


def test_replay_preserves_targets_and_holds_last_without_looping():
    targets = np.zeros((62, 14))
    targets[:, 0] = np.arange(62) / 100
    targets[:, 6] = .4
    replay = ReplayPolicy(targets)
    obs = {"observation.state": targets[0]}
    first, second, last = [replay.infer({**obs, "_replay_cursor": {"next_frame": n, "epoch": 1}})
                           for n in (0, 50, 62)]
    np.testing.assert_array_equal(first["actions"], targets[:50])
    np.testing.assert_array_equal(second["actions"][:12], targets[50:])
    np.testing.assert_array_equal(second["actions"][12:], np.tile(targets[-1], (38, 1)))
    np.testing.assert_array_equal(last["actions"], np.tile(targets[-1], (50, 1)))
    assert last["server_timing"]["source"] == "recorded_replay"
    assert last["server_timing"]["rtc_used"] is False


def test_replay_refuses_rtc_and_unaligned_start_without_consuming_frames():
    replay = ReplayPolicy(np.zeros((50, 14)))
    with pytest.raises(ValueError, match="sync_hold"):
        replay.infer({"rtc": {}})
    result = replay.infer({"observation.state": np.ones(14),
                           "_replay_cursor": {"next_frame": 0, "epoch": 1}})
    assert result["server_timing"]["replay_refused"]
    np.testing.assert_array_equal(result["actions"], np.ones((50, 14)))
    assert replay.epoch is None
    assert replay.cursor == 0


def test_replay_pause_resumes_executed_frame_not_end_of_sent_block():
    targets = np.zeros((100, 14))
    targets[:, 0] = np.arange(100) * .01
    replay = ReplayPolicy(targets)
    def request(frame, epoch):
        return {"observation.state": targets[frame],
                "_replay_cursor": {"next_frame": frame, "epoch": epoch}}
    replay.infer(request(0, 1))  # Sending 50 does not commit any execution.
    np.testing.assert_array_equal(replay.infer(request(0, 1))["actions"], targets[:50])
    np.testing.assert_array_equal(replay.infer(request(7, 2))["actions"], targets[7:57])
    wrong_pose = request(7, 3)
    wrong_pose["observation.state"] = np.ones(14)
    assert replay.infer(wrong_pose)["server_timing"]["replay_refused"]
    assert replay.cursor == 7 and replay.epoch == 2


def test_session_cursor_only_acknowledges_submitted_active_decisions():
    from types import SimpleNamespace

    from yam_abc_reproduce.hil.session import Session
    s = Session(None)
    s._replay_block = ("active", 50, 80)
    d = SimpleNamespace(source="policy", request="stale", action_index=9)
    s.submitted(d)
    assert s.replay_next_frame == 0
    d.request = "active"
    s.submitted(d)
    assert s.replay_next_frame == 60
    d.source = "hold"
    d.action_index = 20
    s.submitted(d)
    assert s.replay_next_frame == 60
    s.rewind_replay()
    assert s.replay_next_frame == 0 and s._replay_block is None


def test_leader_gain_changes_without_manual_mode_transition():
    from types import SimpleNamespace

    from yam_abc_reproduce.robot.yam_adapter import YamLeaderArm
    calls = []
    arm = YamLeaderArm.__new__(YamLeaderArm)
    arm._n, arm._hil_manual, arm._hil_gain_scale = 6, False, .2
    arm._native_kp, arm._native_kd = np.ones(6) * 10, np.ones(6)
    arm._robot = SimpleNamespace(
        update_kp_kd=lambda **kw: calls.append(kw),
        get_joint_pos=lambda: np.zeros(6), command_joint_pos=lambda _: None,
    )
    arm.set_manual_control(False, .4)
    np.testing.assert_array_equal(calls[-1]["kp"], np.ones(6) * 4)
    arm.set_manual_control(False, .2)
    np.testing.assert_array_equal(calls[-1]["kp"], np.ones(6) * 2)


def test_session_pause_resume_replays_next_unsubmitted_frame():
    from yam_abc_reproduce.hil.core import Arbiter, Mode
    from yam_abc_reproduce.hil.policy import Reply
    from yam_abc_reproduce.hil.session import Session

    targets = np.zeros((100, 14))
    targets[:, 0] = np.arange(100) * .001
    replay = ReplayPolicy(targets)

    class Worker:
        planner_alive = True
        reply = None

        def poll(self):
            reply, self.reply = self.reply, None
            return reply

        def submit(self, token, obs):
            result = replay.infer(obs)
            self.reply = Reply(token, result["actions"], server_timing=result["server_timing"])
            return True

    session = Session(Arbiter(Mode.INFERENCE, streaming=False, execute_steps=50,
                              policy_fusion="sync_hold"), Worker())
    q = np.zeros(14)
    def tick(n, event=None):
        nonlocal q
        d = session.tick(q, q, now=n / 30, dt=1 / 30, observation_id=n,
                         observation={"observation.state": q.copy()}, event=event,
                         leader_ready=True)
        q = d.action.copy()
        session.submitted(d)
        return d

    tick(0, "start")
    for n in range(1, 8):
        assert tick(n).action[0] == pytest.approx(targets[n - 1, 0])
    tick(8, "hold")
    for n in range(9, 20):
        tick(n)
    assert session.replay_next_frame == 7
    tick(20, "start")
    assert tick(21).action[0] == pytest.approx(targets[7, 0])
    assert session.replay_next_frame == 8


def test_replay_loading_uses_submitted_not_leader_or_model(tmp_path, monkeypatch):
    from yam_abc_reproduce.hil import replay_policy

    (tmp_path / "manifest.json").write_text(json.dumps({"fps": 30, "outcome": "unknown"}))
    rows = [{"tick": n, "submitted_action": [.1] * 14,
             "leader_state": [.8] * 14, "policy_action": [.5] * 14} for n in range(3)]
    monkeypatch.setattr(replay_policy, "read_rows", lambda _: iter(rows))
    np.testing.assert_allclose(load_targets(tmp_path, start=1, steps=2), .1)
    assert load_targets(tmp_path, steps=None).shape == (3, 14)
    rows[-1]["tick"] = 4
    with pytest.raises(ValueError, match="tick gap"):
        load_targets(tmp_path)


def test_xr1_eef_replay_roundtrip_uses_recorded_observation(tmp_path, monkeypatch):
    from yam_abc_reproduce.hil import replay_policy

    (tmp_path / "manifest.json").write_text(json.dumps({"fps": 30, "outcome": "unknown"}))
    observed = np.array(
        [0.2, 1.1, 1.0, -0.3, 0.1, 0.2, 0.7, -0.1, 1.4, 0.8, 0.2, -0.1, 0.3, 0.3]
    )
    recorded = np.tile(observed, (35, 1))
    recorded[:, 0] += np.linspace(0, 0.02, 35)
    recorded[:, 6] = np.linspace(0.7, 0.2, 35)
    rows = [
        {
            "tick": i,
            "observation_state": observed.copy(),
            "submitted_action": target.copy(),
        }
        for i, target in enumerate(recorded)
    ]
    monkeypatch.setattr(replay_policy, "read_rows", lambda _: iter(rows))
    converted = load_xr1_eef_targets(tmp_path, steps=None)
    assert converted.shape == recorded.shape
    np.testing.assert_allclose(converted[:, [6, 13]], recorded[:, [6, 13]], atol=1e-6)
    assert np.max(np.abs(converted - recorded)) < 0.002
    replay = ReplayPolicy(converted, action_representation="xr1_eef_delta_roundtrip")
    reply = replay.infer(
        {"observation.state": converted[0], "_replay_cursor": {"next_frame": 0, "epoch": 1}}
    )
    assert reply["server_timing"]["action_representation"] == "xr1_eef_delta_roundtrip"


def test_replay_preserves_grippers_through_worker_and_real_planner_process():
    from yam_abc_reproduce.hil.core import Arbiter, Mode
    from yam_abc_reproduce.hil.planner_process import ProcessActionPlanner
    from yam_abc_reproduce.hil.policy import PolicyWorker
    from yam_abc_reproduce.hil.session import Session

    targets = np.zeros((50, 14))
    targets[:, 6] = np.linspace(0, 0.29, 50)
    targets[:, 13] = 0.25
    q = targets[0].copy()
    arbiter = Arbiter(
        Mode.INFERENCE, streaming=True, policy_fusion="sync_hold",
        external_planner=True, max_request_age=3,
    )
    worker = PolicyWorker(ReplayPolicy(targets), planner=ProcessActionPlanner())
    worker.plan_context = lambda: ("sync_hold", None, 1 / 30, 3)
    session = Session(arbiter, worker)
    try:
        deadline = time.monotonic() + 5
        while not worker.ready and time.monotonic() < deadline:
            time.sleep(0.001)
        assert worker.ready
        now = time.monotonic()
        session.tick(
            q, q, now=now, dt=1 / 30, observation_id=1, observed_at=now,
            observation={"observation.state": q.copy()}, event="start",
        )
        while worker._replies.empty() and time.monotonic() < deadline:
            time.sleep(0.001)
        assert not worker._replies.empty()
        now = time.monotonic()
        for index in range(50):
            decision = session.tick(
                q, q, now=now + index / 30, dt=1 / 30, observation_id=index + 2,
            )
            np.testing.assert_allclose(decision.action, targets[index], rtol=0, atol=0)
            session.submitted(decision)
            q = decision.action.copy()
        assert session.replay_next_frame == 50
    finally:
        worker.close()
