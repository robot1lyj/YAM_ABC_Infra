"""Explicit handback uses physical commitments, with no Thor extension or motors."""

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from yam_abc_reproduce.hil.core import Arbiter, Mode, Phase
from yam_abc_reproduce.hil.human_rtc import HumanRtc, HumanRtcConfig, build_handover_targets
from yam_abc_reproduce.hil.policy import PolicyWorker, Reply
from yam_abc_reproduce.hil.run import MockPolicy
from yam_abc_reproduce.hil.session import Session


def limit(action):
    out = np.clip(action, -1, 1)
    out[[6, 13]] = np.clip(out[[6, 13]], .02, .98)
    return out


def manual_session(config=None):
    q = np.full(14, .2)
    a = Arbiter(Mode.HIL, policy_fusion="rtc", max_request_age=1, tick_timeout=1)
    a.start(q)
    a.takeover(q, q)
    a.manual_ready(q, q)
    human = HumanRtc(config or HumanRtcConfig())
    human.refresh(a)
    s = Session(a, rtc_limit_target=limit, human_rtc=human)
    for tick in range(10, 13):
        q = q.copy()
        q[0] += .01
        # Follower grip targets include the existing soft pickup and SDK limits.
        a.rtc_timeline.record_submitted(tick, q)
        human.record_submitted(a, q, tick=tick, now=tick/30)
    return s, human, q


def handback(s, q, *, tick=13, event="resume_policy"):
    return s.tick(q, q, now=tick/30, dt=1/30, observation_id=tick,
                  policy_tick=tick, event=event)


def test_decay_uses_actual_sample_time_joint_limits_and_holds_grippers():
    cfg = HumanRtcConfig(max_velocity=.5)
    latest, previous = np.full(14, .99), np.full(14, .89)
    targets = build_handover_targets(latest, previous, sample_dt=.1, action_dt=1/30,
        steps=6, config=cfg, limit_target=limit)
    assert targets[0, 0] == pytest.approx(1)
    np.testing.assert_array_equal(targets[:, [6, 13]], np.full((6, 2), .98))
    np.testing.assert_array_equal(targets[-1], targets[-2])
    latest[:] = .2
    previous[:] = .19
    targets = build_handover_targets(latest, previous, sample_dt=1/30, action_dt=1/30,
        steps=6, config=cfg, limit_target=limit)
    steps = np.diff(np.vstack((latest, targets)), axis=0)[:, 0]
    assert 0 < steps[0] < .01
    assert np.all(np.diff(steps) <= 1e-12) and steps[-1] == 0
    np.testing.assert_allclose(targets[:, [6, 13]], .2)


@pytest.mark.parametrize("settings", [{"enable_human_rtc": 1}, {"enable_human_rtc": "false"},
    {"human_rtc_velocity_decay_steps": 0}, {"human_rtc_velocity_decay_steps": 11},
    {"human_rtc_max_velocity": float("nan")}, {"human_rtc_max_sample_age": True}])
def test_bad_configuration_is_rejected(settings):
    with pytest.raises(ValueError):
        HumanRtcConfig.from_settings(settings)


def test_manual_control_remains_free_and_has_no_future_commitments():
    s, human, q = manual_session()
    a = s.arbiter
    q = q.copy()
    q[0] += .3
    d = s.tick(q, q, now=13/30, dt=1/30, observation_id=13, policy_tick=13)
    assert d.source == "human" and d.intervention
    a.rtc_timeline.record_submitted(13, d.action)
    human.record_submitted(a, d.action, tick=13, now=13/30)
    assert not a.rtc_timeline.has_target(14)
    assert a.rtc_timeline.pending is None and a.pending is None


def test_handover_preserves_real_history_then_commits_exact_executable_motion():
    s, human, q = manual_session()
    a = s.arbiter
    epoch = a.epoch
    d = handback(s, q)
    assert a.epoch == epoch + 1 and a.phase == Phase.RESUME
    assert d.source == "handover" and not d.intervention and not d.policy_valid
    assert d.policy_action is None and not d.leader_manual
    assert d.action[0] > q[0]
    assert d.policy_selection["handover"]["samples"][-1]["tick"] == 12
    assert human.handovers == 1
    a.rtc_timeline.record_submitted(13, d.action)
    token, commitment = a.request_rtc(13, 13/30, 11/30, 11, 13, limit)
    assert commitment.takeover_tick == 20
    np.testing.assert_allclose(commitment.actions[:3, 0], [.22, .23, d.action[0]])
    # The future tail holds the END of the planned deceleration, not its first row.
    np.testing.assert_array_equal(commitment.actions[-1], commitment.actions[-2])
    rows = np.tile(commitment.actions[-1], (50, 1))
    rows[:9] = commitment.actions
    rows[9:, 0] += .01
    assert a.accept_rtc(token, rows, 17/30, 17, limit)
    for tick in range(14, 20):
        decision = s.tick(q, q, now=tick/30, dt=1/30, observation_id=tick, policy_tick=tick)
        np.testing.assert_allclose(decision.action, commitment.actions[tick-11], atol=2e-6)
        assert not decision.policy_valid
        a.rtc_timeline.record_submitted(tick, decision.action)
    decision = s.tick(q, q, now=20/30, dt=1/30, observation_id=20, policy_tick=20)
    assert decision.source == "policy" and decision.phase == Phase.POLICY
    np.testing.assert_allclose(decision.action, rows[9])


@pytest.mark.parametrize("event", ["hold", "stop", "takeover", "mode:inference", "end_intervention:collect"])
def test_pause_stop_takeover_and_mode_change_cancel_continuation_and_old_replies(event):
    s, _, q = manual_session()
    a = s.arbiter
    d = handback(s, q)
    a.rtc_timeline.record_submitted(13, d.action)
    token, commitment = a.request_rtc(13, 13/30, 13/30, 13, 13, limit)
    s.tick(q, q, now=14/30, dt=1/30, observation_id=14, policy_tick=14, event=event)
    assert not a.rtc_timeline.has_target(15)
    rows = np.tile(q, (50, 1))
    rows[:9] = commitment.actions
    assert not a.accept_rtc(token, rows, 15/30, 15, limit)


def test_late_reply_does_not_retime_or_extend_the_deceleration():
    s, _, q = manual_session()
    a = s.arbiter
    d = handback(s, q)
    a.rtc_timeline.record_submitted(13, d.action)
    token, commitment = a.request_rtc(13, 13/30, 13/30, 13, 13, limit)
    for tick in range(14, 24):
        d = s.tick(q, q, now=tick/30, dt=1/30, observation_id=tick, policy_tick=tick)
        assert d.source != "policy"
        a.rtc_timeline.record_submitted(tick, d.action)
    final = d.action.copy()
    rows = np.tile(q, (50, 1))
    rows[:9] = commitment.actions
    assert not a.accept_rtc(token, rows, 24/30, 24, limit)
    d = s.tick(q, q, now=24/30, dt=1/30, observation_id=24, policy_tick=24)
    np.testing.assert_array_equal(d.action, final)
    assert d.source == "hold"


def test_original_takeover_tick_can_replace_the_uncommitted_tail_of_handover():
    s, _, q = manual_session()
    a = s.arbiter
    a.rtc_timeline.delay_steps = 4
    d = handback(s, q)
    a.rtc_timeline.record_submitted(13, d.action)
    token, commitment = a.request_rtc(13, 13/30, 11/30, 11, 13, limit)
    assert commitment.takeover_tick == 15
    rows = np.tile(commitment.actions[-1], (50, 1))
    rows[:4] = commitment.actions
    rows[4:, 0] += .01
    assert a.accept_rtc(token, rows, 14/30, 14, limit)
    for tick in (14, 15):
        d = s.tick(q, q, now=tick/30, dt=1/30, observation_id=tick, policy_tick=tick)
        a.rtc_timeline.record_submitted(tick, d.action)
    assert d.source == "policy"
    np.testing.assert_allclose(d.action, rows[4])
    assert a.rtc_timeline.select(16, q)[1] == "policy"


@pytest.mark.parametrize("change", ["disabled", "keyboard", "leaders", "pld", "not_ready", "sample_gap", "stale"])
def test_unavailable_continuation_falls_back_without_a_new_server_capability(change):
    s, human, q = manual_session(HumanRtcConfig(enabled=change != "disabled"))
    a = s.arbiter
    if change == "keyboard":
        a.hil_input = "keyboard"
    human.refresh(a, leaders_connected=change != "leaders", pld=change == "pld",
                  ready=change != "not_ready")
    tick = 13
    if change == "sample_gap":
        human.record_submitted(a, q, tick=20, now=20/30)
        tick = 21
    if change == "stale":
        tick = 30
    d = handback(s, q, tick=tick)
    assert a.phase == Phase.RESUME and d.source == "hold"
    assert not d.policy_valid and not a.rtc_timeline.has_target(tick+1)
    np.testing.assert_array_equal(d.action, q)


def test_policy_fault_blocks_handback_and_preserves_manual_control():
    s, _, q = manual_session()
    s.policy_error = "transport unavailable"
    d = handback(s, q)
    assert s.arbiter.phase == Phase.HUMAN and d.source == "human"


def test_old_epoch_reply_cannot_replace_the_handover():
    s, _, q = manual_session()
    a = s.arbiter
    from yam_abc_reproduce.hil.core import Request
    token = Request(a.epoch-1, 100, 1, 0, observation_policy_tick=0, rtc_delay_steps=9)
    reply = Reply(token, np.tile(q + .2, (50, 1)))
    s.worker = SimpleNamespace(ready=True, poll=lambda: reply, planner_alive=True)
    d = handback(s, q)
    assert d.source == "handover" and s.last_reply["discarded"]
    assert not a.rtc_timeline.has_accepted_plan


def test_runtime_primary_button_hands_back_and_records_real_prefixes(tmp_path):
    from yam_abc_reproduce.camera.mock_camera import MockCamera
    from yam_abc_reproduce.camera.worker import CameraWorker
    from yam_abc_reproduce.config import StationConfig
    from yam_abc_reproduce.hil.lerobot_export import expert_groups, export_session
    from yam_abc_reproduce.hil.recording import RecordingSession
    from yam_abc_reproduce.hil.run import Runtime
    from yam_abc_reproduce.hil.station import StationIO
    from yam_abc_reproduce.hil.storage import read_rows
    from yam_abc_reproduce.runtime import build_arm_units

    class TrackingPolicy(MockPolicy):
        def __init__(self):
            super().__init__(rtc=True)
            self.requests = []

        def infer_rtc(self, obs, *, target_start_tick, committed_actions):
            # Deliberately only the original RTC signature; no human_rtc metadata/API.
            self.requests.append((target_start_tick, committed_actions.copy()))
            return super().infer_rtc(obs, target_start_tick=target_start_tick, committed_actions=committed_actions)

    policy = TrackingPolicy()
    io = StationIO(build_arm_units(StationConfig(), mock=True), mock=True)
    pressed = threading.Event()
    read = io.read
    manual_reads = 0

    def with_buttons():
        nonlocal manual_reads
        q, leader, _, ages = read()
        if runtime.session.arbiter.phase == Phase.HUMAN:
            manual_reads += 1
            leader[0] = .005 * manual_reads
        return q, leader, [[False, False], [pressed.is_set(), False]], ages

    io.read = with_buttons
    cameras = [CameraWorker(MockCamera(r, r, width=32, height=32)) for r in ("top", "left", "right")]
    worker = PolicyWorker(policy)
    recorder = RecordingSession(tmp_path / "human", video_backend="libx264",
        metadata={"mock": True, "station": {"task_name": "handover test"}})
    runtime = Runtime(io, cameras, worker, recorder, mode="hil",
        settings={"policy_fusion": "rtc", "tick_timeout": 2, "replan_period": .1})
    failures = []

    def wait_for(predicate):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            if predicate():
                return
            if runtime.status.get("error"):
                raise AssertionError(runtime.status)
            time.sleep(.01)
        raise AssertionError(runtime.status)

    def operator():
        try:
            wait_for(lambda: runtime.status.get("phase") == "policy")
            runtime.event("takeover")
            wait_for(lambda: runtime.status.get("phase") == "takeover")
            pressed.set()
            wait_for(lambda: runtime.status.get("phase") == "human")
            pressed.clear()
            count = len(policy.requests)
            wait_for(lambda: manual_reads >= 12)
            assert len(policy.requests) == count  # No RPC competes with handback during manual.
            pressed.set()
            wait_for(lambda: runtime.status.get("human_rtc", {}).get("handovers", 0) == 1)
            pressed.clear()
            wait_for(lambda: runtime.status.get("phase") == "policy")
            wait_for(lambda: runtime.status.get("source") == "policy")
            runtime.event("hold")
            wait_for(lambda: runtime.status.get("phase") == "hold")
        except Exception as exc:
            failures.append(exc)
        finally:
            runtime.stopping.set()

    driver = threading.Thread(target=operator)
    try:
        for camera in cameras:
            camera.start()
        driver.start()
        status = runtime.run(duration=10, auto_start=True)
        driver.join(timeout=1)
        assert not failures, failures
        assert not status.get("error"), status
    finally:
        runtime.stopping.set()
        driver.join(timeout=2)
        worker.close()
        recorder.close()
        for camera in cameras:
            camera.stop()
        io.close()
    assert len(recorder.episodes) == 1
    rows = list(read_rows(recorder.path / recorder.episodes[0]["path"]))
    by_tick = {row["tick"]: row for row in rows}
    bridge = [row for row in rows if row["source"] == "handover"]
    assert len(bridge) == 6
    assert all(not row["expert_valid"] and not row["policy_valid"] and row["policy_action"] is None for row in bridge)
    assert all(row["handover"]["samples"][-1]["tick"] == bridge[0]["tick"]-1 for row in bridge)
    accepted_starts = {row["policy_reply"]["token"]["observation_policy_tick"] for row in rows
        if row.get("policy_reply") and not row["policy_reply"]["discarded"]
        and row["policy_reply"]["token"]["epoch"] == bridge[0]["epoch"]}
    assert accepted_starts
    for start, prefix in policy.requests:
        if start not in accepted_starts:
            continue
        for index, action in enumerate(prefix):
            if start + index in by_tick and by_tick[start + index]["epoch"] <= bridge[0]["epoch"]:
                np.testing.assert_allclose(by_tick[start + index]["submitted_action"], action, atol=2e-6)
    assert not any(row["source"] == "handover" for _, row in expert_groups(recorder.path / recorder.episodes[0]["path"]))
    report = export_session(recorder.path, tmp_path / "export")
    assert report["frames"] == len(rows) and report["action_source"]["3"] == "handover"
