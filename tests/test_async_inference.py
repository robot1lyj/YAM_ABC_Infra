"""Non-RTC inference buffering with simulated Thor latency; no hardware or model."""


import numpy as np
import pytest

from yam_abc_reproduce.hil.core import Arbiter, Mode, Phase
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


def test_finite_thor_gripper_overshoot_is_clamped_on_control_side():
    q = np.zeros(14)
    arbiter = Arbiter(Mode.INFERENCE, max_action_age=3, policy_fusion="sync_hold")
    arbiter.start(q)
    rows = chunk(gripper=1.2)
    rows[:, 13] = -0.3
    policy(arbiter, rows, observed_at=1, sent_at=1.01, received_at=1.05)
    decision = arbiter.step(q, q, now=1.05, dt=0.03, leader_ready=True)
    assert decision.policy_action[[6, 13]].tolist() == [1.0, 0.0]
    assert decision.action[6] <= 1.0 and decision.action[13] >= 0.0


def test_nonfinite_thor_gripper_still_rejected():
    q = np.zeros(14)
    arbiter = Arbiter(Mode.INFERENCE, policy_fusion="sync_hold")
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


def test_action_dt_cli_override_is_checked_without_model_or_motors(capsys):
    import json

    from yam_abc_reproduce.hil.run import main

    main(["--mock", "--mode", "inference", "--policy-fusion", "sync_hold",
          "--action-dt", "0.05", "--check"])
    assert json.loads(capsys.readouterr().out)["action_dt"] == pytest.approx(0.05)


def test_station_defaults_to_rtc_and_explicit_sync_executes_full_chunk(capsys):
    import json
    from pathlib import Path

    import yaml

    from yam_abc_reproduce.hil.run import main

    station = yaml.safe_load((Path(__file__).parents[1] / "configs/station_hil.yaml").read_text())
    assert station["hil"]["policy_fusion"] == "rtc"
    assert station["hil"]["policy_trajectory_hz"] == 0
    main(["--mock", "--mode", "inference", "--check"])
    assert json.loads(capsys.readouterr().out)["policy_fusion"] == "rtc"
    main(["--mock", "--mode", "inference", "--policy-fusion", "sync_hold", "--check"])
    assert json.loads(capsys.readouterr().out)["policy_fusion"] == "sync_hold"
    with pytest.raises(SystemExit):
        main(["--mock", "--mode", "inference", "--policy-fusion", "raw", "--check"])
    main(["--mock", "--mode", "inference", "--policy-fusion", "rtc", "--check"])
    assert json.loads(capsys.readouterr().out)["policy_fusion"] == "rtc"


def test_latency_budget_uses_observation_age_not_only_request_rtt():
    q = np.zeros(14)
    arbiter = Arbiter(
        Mode.INFERENCE, action_dt=0.1,
        expected_policy_latency=0.05, prefetch_margin=0.02,
        max_action_age=3, policy_fusion="sync_hold")
    arbiter.start(q)
    token = arbiter.request(1, 1.10, observed_at=1.0)
    assert token is not None
    assert arbiter.accept(token, chunk(), 1.25)
    assert arbiter.observed_policy_rtt_p95 == pytest.approx(0.15)
    assert arbiter.observed_observation_to_ready_p95 == pytest.approx(0.25)
    assert arbiter.policy_latency_budget == pytest.approx(0.27)


def test_latency_summaries_update_per_reply_not_per_status_read(monkeypatch):
    q = np.zeros(14)
    arbiter = Arbiter(Mode.INFERENCE, policy_fusion="sync_hold")
    calls = []
    percentile = np.percentile

    def count_percentile(*args, **kwargs):
        calls.append(True)
        return percentile(*args, **kwargs)

    monkeypatch.setattr(np, "percentile", count_percentile)
    for index in range(20):
        arbiter.hold(q)
        arbiter.start(q)
        token = arbiter.request(index, index + 1, index + .95)
        assert arbiter.accept(token, chunk(), index + 1.2 + index * .001)
    count = len(calls)
    for _ in range(30):
        assert arbiter.observed_policy_rtt_p95 == pytest.approx(percentile(
            [.2 + i * .001 for i in range(4, 20)], 95))
        assert arbiter.observed_observation_to_ready_p95 == pytest.approx(
            arbiter.observed_policy_rtt_p95 + .05)
        assert arbiter.policy_latency_budget > 0
    assert len(calls) == count == 20


def test_sync_hold_finishes_full_fifty_actions_then_holds_until_next_reply():
    q = np.zeros(14)
    arbiter = Arbiter(
        Mode.INFERENCE, policy_fusion="sync_hold",
        action_dt=0.1, max_action_age=3,
    )
    assert not arbiter.streaming
    assert arbiter.policy_fusion == "sync_hold"
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
        Mode.INFERENCE, policy_fusion="sync_hold",
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
    with pytest.raises(ValueError, match="sync_hold or rtc"):
        Arbiter(Mode.INFERENCE, policy_fusion="ensemble")


@pytest.mark.parametrize("event", ("hold", "stop", "mode:teleop"))
def test_local_reset_clears_plan_and_rejects_old_network_result(event):
    q = np.zeros(14)
    arbiter = Arbiter(Mode.INFERENCE, action_dt=0.1, policy_fusion="sync_hold")
    arbiter.start(q)
    policy(arbiter, chunk(0.2), observed_at=0, sent_at=0, received_at=0.01)
    for index in range(50):
        arbiter.step(q, q, now=.01 + index * .1, dt=.1)
    old = arbiter.request(2, 5.0, observed_at=4.99)
    assert old is not None
    Session(arbiter).tick(q, q, now=5.01, dt=0.03, observation_id=3, event=event)
    assert arbiter._chunk is None
    assert not arbiter.accept(old, chunk(1), 5.02)
    decision = arbiter.step(q, q, now=5.03, dt=0.03)
    assert not decision.policy_valid


def test_request_timeout_and_buffer_exhaustion_hold_instead_of_replaying_tail():
    q = np.zeros(14)
    arbiter = Arbiter(
        Mode.INFERENCE, action_dt=0.1,
        max_request_age=0.5, max_action_age=10, policy_fusion="sync_hold")
    arbiter.start(q)
    policy(arbiter, chunk(0.2), observed_at=0, sent_at=0, received_at=0.01)
    for step in range(50):
        assert arbiter.step(q, q, now=step / 10, dt=0.03, leader_ready=True).policy_valid
    assert not arbiter.step(q, q, now=5.1, dt=0.03, leader_ready=True).policy_valid
    # A completed synchronous block remains ready to request the next block,
    # holding its final target; it is not a fault or an operator pause.
    assert arbiter.phase == Phase.POLICY
    arbiter.start(q)
    arbiter.request(2, 5.2, observed_at=5.2)
    assert arbiter.step(q, q, now=5.8, dt=0.03, leader_ready=True).phase == Phase.HOLD
