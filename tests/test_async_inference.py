"""Non-RTC inference buffering with simulated Thor latency; no hardware or model."""

import threading
import time

import numpy as np
import pytest

from yam_abc_reproduce.hil.core import Arbiter, Mode, Phase
from yam_abc_reproduce.hil.policy import PolicyWorker
from yam_abc_reproduce.hil.session import Session


def chunk(joint=0.0, gripper=0.0):
    rows = np.zeros((50, 14))
    rows[:, 0] = joint
    rows[:, 6] = gripper
    rows[:, 13] = gripper
    return rows


def policy(arbiter, actions, *, observed_at, sent_at, received_at):
    token = arbiter.request(1, sent_at, observed_at=observed_at)
    assert token is not None
    assert arbiter.accept(token, actions, received_at)
    return token


def test_latency_trim_uses_observation_clock_and_drops_expired_prefix():
    q = np.zeros(14)
    arbiter = Arbiter(Mode.INFERENCE, streaming=True, action_dt=0.1, max_action_age=3)
    arbiter.start(q)
    rows = chunk()
    rows[:, 0] = np.arange(50) / 100
    policy(arbiter, rows, observed_at=1.0, sent_at=1.02, received_at=1.36)
    assert arbiter.action_buffer.chunk.first_index == 3
    assert len(arbiter.action_buffer.chunk.actions) == 47
    decision = arbiter.step(q, q, now=1.36, dt=0.03, leader_ready=True)
    assert decision.action_index == 3
    assert decision.policy_action[0] == pytest.approx(0.03)
    assert decision.policy_action[0] != rows[0, 0]


def test_naive_async_replaces_old_chunk_for_joints_and_grippers():
    q = np.zeros(14)
    arbiter = Arbiter(
        Mode.INFERENCE, streaming=True, action_dt=0.1,
        policy_fusion="raw", replan_period=0.2, max_action_age=3,
    )
    arbiter.start(q)
    old = chunk(joint=0.2, gripper=0.0)
    policy(arbiter, old, observed_at=1.0, sent_at=1.01, received_at=1.05)
    new = chunk(joint=0.8, gripper=1.0)
    token = arbiter.request(2, 1.25, observed_at=1.24)
    assert token is not None
    assert arbiter.accept(token, new, 1.55)
    assert arbiter.action_buffer.chunk.first_index == 3
    decision = arbiter.step(q, q, now=1.55, dt=0.03, leader_ready=True)
    assert decision.policy_action[0] == pytest.approx(0.8)
    assert decision.policy_action[[6, 13]].tolist() == [1.0, 1.0]
    assert decision.policy_selection["joint_sources"][0]["request_id"] == token.request_id
    assert decision.policy_selection["gripper_source"]["request_id"] == token.request_id


def test_finite_thor_gripper_overshoot_is_clamped_on_control_side():
    q = np.zeros(14)
    arbiter = Arbiter(Mode.INFERENCE, streaming=True, max_action_age=3)
    arbiter.start(q)
    rows = chunk(gripper=1.2)
    rows[:, 13] = -0.3
    policy(arbiter, rows, observed_at=1, sent_at=1.01, received_at=1.05)
    decision = arbiter.step(q, q, now=1.05, dt=0.03, leader_ready=True)
    assert decision.policy_action[[6, 13]].tolist() == [1.0, 0.0]
    assert decision.action[6] <= 1.0 and decision.action[13] >= 0.0


def test_nonfinite_thor_gripper_still_rejected():
    q = np.zeros(14)
    arbiter = Arbiter(Mode.INFERENCE, streaming=True)
    arbiter.start(q)
    token = arbiter.request(1, 1.01, observed_at=1)
    rows = chunk()
    rows[:, 6] = np.nan
    with pytest.raises(ValueError, match="finite"):
        arbiter.accept(token, rows, 1.05)


def test_station_io_clamps_absolute_joint_targets_to_sdk_limits():
    from types import SimpleNamespace

    from yam_abc_reproduce.config import StationConfig
    from yam_abc_reproduce.hil.station import StationIO
    from yam_abc_reproduce.runtime import build_arm_units

    io = StationIO(build_arm_units(StationConfig(), mock=True), mock=True)
    try:
        target = np.zeros(14)
        target[0], target[7] = 4 * np.pi, -4 * np.pi
        decision = SimpleNamespace(
            action=target, source="policy", leader_manual=True, leader_freeze=False,
        )
        submitted, _ = io.apply(
            decision, np.zeros(14), np.zeros(14), dt=0.03,
        )
        assert submitted[0] == pytest.approx(np.pi)
        assert submitted[7] == pytest.approx(-np.pi)
    finally:
        io.close()


def test_deadline_prefetch_can_override_long_freshness_interval():
    q = np.zeros(14)
    arbiter = Arbiter(
        Mode.INFERENCE, streaming=True, action_dt=0.1,
        replan_period=1.0, max_action_age=1.0,
        expected_policy_latency=0.25, prefetch_margin=0.05,
    )
    arbiter.start(q)
    first = arbiter.request(1, 1.0, observed_at=1.0)
    assert first is not None and arbiter.last_request_reason == "deadline"
    arbiter._last_request_at = 1.0
    assert arbiter.accept(first, chunk(), 1.02)
    assert arbiter.action_buffer.seconds_to_expiry(1.6) == pytest.approx(0.4)
    assert arbiter.request(2, 1.6, observed_at=1.6) is None
    second = arbiter.request(3, 1.75, observed_at=1.75)
    assert second is not None and arbiter.last_request_reason == "deadline"
    arbiter._last_request_at = 1.75
    assert arbiter.accept(second, chunk(), 2.15)  # simulated 400ms Thor/network jitter
    assert arbiter.observed_policy_rtt_p95 == pytest.approx(0.381, abs=0.01)
    assert arbiter.observed_observation_to_ready_p95 == pytest.approx(0.381, abs=0.01)
    assert arbiter.policy_latency_budget > 0.42
    # The latest observation's old prefix (four 100ms steps) was not executed.
    assert arbiter.action_buffer.chunk.first_index == 4
    assert arbiter.request(4, 2.3, observed_at=2.3) is None
    third = arbiter.request(5, 2.35, observed_at=2.35)
    assert third is not None and arbiter.last_request_reason == "deadline"


def test_action_dt_cli_override_is_checked_without_model_or_motors(capsys):
    import json

    from yam_abc_reproduce.hil.run import main

    main(["--mock", "--mode", "inference", "--policy-fusion", "sync_hold",
          "--action-dt", "0.05", "--check"])
    assert json.loads(capsys.readouterr().out)["action_dt"] == pytest.approx(0.05)


def test_station_defaults_to_rtc_and_baseline_executes_full_chunk(capsys):
    import json
    from pathlib import Path

    import yaml

    from yam_abc_reproduce.hil.run import main

    station = yaml.safe_load((Path(__file__).parents[1] / "configs/station_hil.yaml").read_text())
    assert station["hil"]["policy_fusion"] == "rtc"
    assert station["hil"]["policy_trajectory_hz"] == 0
    main(["--mock", "--mode", "inference", "--check"])
    assert json.loads(capsys.readouterr().out)["policy_fusion"] == "rtc"
    main(["--mock", "--mode", "inference", "--baseline", "--check"])
    assert json.loads(capsys.readouterr().out)["policy_fusion"] == "sync_hold"
    with pytest.raises(SystemExit):
        main(["--mock", "--mode", "inference", "--policy-fusion", "raw", "--check"])
    main(["--mock", "--mode", "inference", "--policy-fusion", "rtc", "--check"])
    assert json.loads(capsys.readouterr().out)["policy_fusion"] == "rtc"


def test_latency_budget_uses_observation_age_not_only_request_rtt():
    q = np.zeros(14)
    arbiter = Arbiter(
        Mode.INFERENCE, streaming=True, action_dt=0.1,
        expected_policy_latency=0.05, prefetch_margin=0.02,
        max_action_age=3,
    )
    arbiter.start(q)
    token = arbiter.request(1, 1.10, observed_at=1.0)
    assert token is not None
    assert arbiter.accept(token, chunk(), 1.25)
    assert arbiter.observed_policy_rtt_p95 == pytest.approx(0.15)
    assert arbiter.observed_observation_to_ready_p95 == pytest.approx(0.25)
    assert arbiter.policy_latency_budget == pytest.approx(0.27)


def test_step_ten_replan_runs_old_plan_then_time_aligns_150ms_reply():
    """The control path keeps ticking while one new chunk is in flight."""
    q = np.zeros(14)
    arbiter = Arbiter(
        Mode.INFERENCE, streaming=True, action_dt=1 / 30,
        replan_period=10 / 30, policy_fusion="raw",
        max_action_age=1.5,
    )
    arbiter.start(q)
    old = chunk()
    old[:, 0] = np.arange(50) * 0.01
    policy(arbiter, old, observed_at=0, sent_at=0, received_at=0.12)

    # At about action 10 the cadence launches the only next request.  During
    # its simulated 150 ms flight, the old plan remains continuously usable.
    token = arbiter.request(10, 10 / 30, observed_at=10 / 30)
    assert token is not None and arbiter.last_request_reason == "freshness"
    pending_indices = []
    for now in (0.35, 0.38, 0.41, 0.44, 0.47):
        decision = arbiter.step(q, q, now=now, dt=1 / 30, leader_ready=True)
        assert decision.phase == Phase.POLICY and decision.source == "policy"
        pending_indices.append(decision.action_index)
    assert pending_indices == sorted(pending_indices)

    new = chunk(1.0, 0.8)
    assert arbiter.accept(token, new, 10 / 30 + 0.15)
    assert arbiter.action_buffer.last_trimmed_steps == 4
    decision = arbiter.step(q, q, now=10 / 30 + 0.15, dt=1 / 30, leader_ready=True)
    assert decision.action_index == 4  # never executes the stale index-zero target
    # Raw replacement uses the new plan without a second smoothing method.
    assert decision.policy_action[0] == pytest.approx(1.0)
    assert decision.policy_action[6] == pytest.approx(0.8)
    assert arbiter.action_buffer.last_seam_max_rad == pytest.approx(0.86)
    assert arbiter.action_buffer.last_seam_gripper_max == pytest.approx(0.8)


def test_raw_reply_records_trim_and_target_discontinuity_without_blending():
    from yam_abc_reproduce.hil.policy import Reply

    q = np.zeros(14)
    arbiter = Arbiter(
        Mode.INFERENCE, streaming=True, action_dt=1 / 30,
        policy_fusion="raw", max_action_age=3,
    )
    arbiter.start(q)
    policy(arbiter, chunk(0.2, 0.0), observed_at=0, sent_at=0, received_at=0.05)
    token = arbiter.request(2, 10 / 30, observed_at=10 / 30)
    assert token is not None

    class RepliedWorker:
        def poll(self):
            return Reply(token, chunk(0.8, 1.0), worker_elapsed_ms=150)

        def submit(self, _token, _observation):
            return False

    session = Session(arbiter, RepliedWorker())
    decision = session.tick(
        q, q, now=10 / 30 + 0.15, dt=1 / 30,
        observation_id=3, observation=None, leader_ready=True,
    )
    assert decision.policy_action[0] == pytest.approx(0.8)
    assert decision.policy_action[6] == pytest.approx(1.0)
    assert session.last_reply["trimmed_steps"] == 4
    assert session.last_reply["new_vs_old_target_joint_max_rad"] == pytest.approx(0.6)
    assert session.last_reply["new_vs_old_target_gripper_max"] == pytest.approx(1.0)


def test_sync_hold_finishes_full_fifty_actions_then_holds_until_next_reply():
    q = np.zeros(14)
    arbiter = Arbiter(
        Mode.INFERENCE, streaming=True, policy_fusion="sync_hold",
        action_dt=0.1, max_action_age=3,
    )
    assert not arbiter.streaming
    assert arbiter.action_buffer.fusion == "sync_hold"
    assert arbiter.execute_steps == 50
    arbiter.start(q)
    first = arbiter.request(1, 1.0, observed_at=1.0)
    assert first is not None
    assert arbiter.accept(first, chunk(0.2, 1.0), 1.05)
    for step in range(50):
        decision = arbiter.step(
            q, q, now=1.05 + step * 0.1, dt=0.1, leader_ready=True
        )
        assert decision.source == "policy" and decision.action_index == step
        if step < 49:
            assert arbiter.request(step + 2, 1.05 + step * 0.1) is None
    second = arbiter.request(60, 5.95, observed_at=5.95)
    assert second is not None
    for now in (6.05, 6.15, 6.25):
        decision = arbiter.step(q, q, now=now, dt=0.1, leader_ready=True)
        assert decision.source == "hold" and decision.phase == Phase.POLICY
        assert decision.policy_action is None
    assert arbiter.accept(second, chunk(0.4, 0.0), 6.30)
    assert arbiter.observed_policy_rtt_p95 == pytest.approx(0.335)
    resumed = arbiter.step(q, q, now=6.35, dt=0.1, leader_ready=True)
    assert resumed.source == "policy" and resumed.action_index == 0
    assert resumed.policy_action[0] == pytest.approx(0.4)


def test_sync_hold_full_chunk_has_its_own_execution_deadline():
    q = np.zeros(14)
    arbiter = Arbiter(
        Mode.INFERENCE, streaming=True, policy_fusion="sync_hold",
        action_dt=1 / 30, max_action_age=1.5,
    )
    arbiter.start(q)
    token = arbiter.request(1, 1.0, observed_at=1.0)
    assert token is not None and arbiter.accept(token, chunk(0.1), 1.17)
    # 50/30 exceeds the ordinary asynchronous 1.5 s observation-age limit.
    for step in range(50):
        decision = arbiter.step(
            q, q, now=1.17 + step / 30, dt=1 / 30, leader_ready=True
        )
        assert decision.source == "policy" and decision.action_index == step
    arbiter.hold(q)
    token = arbiter.request(2, 3.0, observed_at=3.0)
    assert token is None
    arbiter.start(q)
    fresh = arbiter.request(3, 3.1, observed_at=3.1)
    assert fresh is not None and arbiter.accept(fresh, chunk(0.1), 3.2)
    expired = arbiter.step(q, q, now=5.0, dt=1 / 30, leader_ready=True)
    assert expired.phase == Phase.HOLD and expired.source == "hold"


def test_retired_multi_chunk_mode_is_rejected():
    with pytest.raises(ValueError, match="raw, tda_smooth, sync_hold or rtc"):
        Arbiter(Mode.INFERENCE, streaming=True, policy_fusion="ensemble")


def test_slow_thor_does_not_block_control_or_queue_intermediate_observations():
    entered = threading.Event()
    release = threading.Event()
    observations = []

    class SlowThor:
        def infer(self, obs):
            observations.append(obs["id"])
            if len(observations) == 2:
                entered.set()
                assert release.wait(2)
            return {"actions": chunk(0.2)}

    worker = PolicyWorker(SlowThor())
    q = np.zeros(14)
    arbiter = Arbiter(
        Mode.INFERENCE, streaming=True, action_dt=0.1,
        replan_period=0.2, max_request_age=1.5, max_action_age=3,
    )
    session = Session(arbiter, worker)

    def tick(now, identity, event=None):
        return session.tick(
            q, q, now=now, dt=0.03, observation_id=identity,
            observation={"id": identity}, observed_at=now,
            leader_ready=True, event=event,
        )

    try:
        tick(0, 1, "start")
        deadline = time.monotonic() + 1
        while worker._replies.empty() and time.monotonic() < deadline:
            time.sleep(0.001)
        assert not worker._replies.empty()
        assert tick(0.05, 2).source == "policy"
        tick(0.25, 3)
        assert entered.wait(1)
        for i, now in enumerate((0.3, 0.4, 0.5, 0.6), start=4):
            started = time.monotonic()
            decision = tick(now, i)
            assert time.monotonic() - started < 0.05
            assert decision.source == "policy" and decision.phase == Phase.POLICY
        assert observations == [1, 3]  # nothing queued while Thor is blocked
        release.set()
        deadline = time.monotonic() + 1
        while worker._replies.empty() and time.monotonic() < deadline:
            time.sleep(0.001)
        assert not worker._replies.empty()
        tick(0.65, 8)  # replan sends the latest snapshot, not 4-7
        deadline = time.monotonic() + 1
        while len(observations) < 3 and time.monotonic() < deadline:
            time.sleep(0.001)
        assert observations == [1, 3, 8]
    finally:
        release.set()
        worker.close()


def test_mock_30hz_runtime_keeps_policy_ticks_during_background_rpc():
    from yam_abc_reproduce.camera.mock_camera import MockCamera
    from yam_abc_reproduce.camera.worker import CameraWorker
    from yam_abc_reproduce.config import StationConfig
    from yam_abc_reproduce.hil.run import Runtime
    from yam_abc_reproduce.hil.station import StationIO
    from yam_abc_reproduce.runtime import build_arm_units

    class MemoryRecorder:
        def __init__(self):
            self.metadata = {}
            self.queue = self
            self.written = 0
            self.rows = []
            self.error = None

        def qsize(self):
            return 0

        def submit(self, row, _images):
            self.rows.append(row)
            self.written += 1
            return True

    class DelayedThor:
        def __init__(self):
            self.intervals = []

        def infer(self, obs):
            started = time.monotonic()
            time.sleep(0.32)  # ten control periods, but under the 1.5s RPC timeout
            self.intervals.append((started, time.monotonic()))
            return {"actions": chunk(0.2)}

    io = StationIO(build_arm_units(StationConfig(), mock=True), mock=True)
    cameras = [
        CameraWorker(MockCamera(role, role, width=16, height=16))
        for role in ("top", "left", "right")
    ]
    client = DelayedThor()
    worker = PolicyWorker(client)
    recorder = MemoryRecorder()
    try:
        for camera in cameras:
            camera.start()
        runtime = Runtime(
            io, cameras, worker, recorder, mode="inference", hz=30,
            action_dt=1 / 30, streaming=True,
            settings={"request_timeout": 1.5, "action_timeout": 1.5},
        )
        status = runtime.run(duration=2.2, auto_start=True)
        assert status["tick"] >= 55 and status["deadline_misses"] < 8, status
        assert not status["error"] and len(client.intervals) >= 2
        assert "policy_metadata" not in recorder.metadata
        second_start, second_end = client.intervals[1]
        during_rpc = [
            row for row in recorder.rows
            if second_start <= row["time"] <= second_end
        ]
        print(
            f"async_mock: tick={status['tick']} misses={status['deadline_misses']} "
            f"policy_ticks_during_second_rpc={len(during_rpc)} "
            f"thor_calls={len(client.intervals)}"
        )
        assert len(during_rpc) >= 5
        assert all(row["source"] == "policy" for row in during_rpc)
    finally:
        for camera in cameras:
            camera.stop()
        worker.close()
        io.close()


@pytest.mark.parametrize("event", ("hold", "stop", "mode:teleop"))
def test_local_reset_clears_plan_and_rejects_old_network_result(event):
    q = np.zeros(14)
    arbiter = Arbiter(Mode.INFERENCE, streaming=True, action_dt=0.1)
    arbiter.start(q)
    policy(arbiter, chunk(0.2), observed_at=0, sent_at=0, received_at=0.01)
    old = arbiter.request(2, 0.3, observed_at=0.29)
    assert old is not None
    Session(arbiter).tick(q, q, now=0.31, dt=0.03, observation_id=3, event=event)
    assert arbiter.action_buffer.remaining(0.31) == 0
    assert not arbiter.accept(old, chunk(1), 0.32)
    decision = arbiter.step(q, q, now=0.33, dt=0.03)
    assert not decision.policy_valid


def test_request_timeout_and_buffer_exhaustion_hold_instead_of_replaying_tail():
    q = np.zeros(14)
    arbiter = Arbiter(
        Mode.INFERENCE, streaming=True, action_dt=0.1,
        max_request_age=0.5, max_action_age=10,
    )
    arbiter.start(q)
    policy(arbiter, chunk(0.2), observed_at=0, sent_at=0, received_at=0.01)
    assert arbiter.step(q, q, now=4.9, dt=0.03, leader_ready=True).policy_valid
    assert not arbiter.step(q, q, now=5.1, dt=0.03, leader_ready=True).policy_valid
    assert arbiter.phase == Phase.HOLD
    arbiter.start(q)
    arbiter.request(2, 5.2, observed_at=5.2)
    assert arbiter.step(q, q, now=5.8, dt=0.03, leader_ready=True).phase == Phase.HOLD
