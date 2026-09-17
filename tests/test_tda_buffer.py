"""OpenArm TDA queue rule, exercised with YAM 14D actions and no hardware."""

import numpy as np
import pytest

from yam_abc_reproduce.hil.core import Arbiter, Mode, Phase, Request
from yam_abc_reproduce.hil.tda_buffer import TdaActionBuffer


def rows(joint, gripper=0):
    action = np.zeros((50, 14))
    action[:, 0] = joint
    action[:, 6] = gripper
    return action


def test_tda_drops_consumed_request_steps_and_blends_entire_overlap():
    buffer = TdaActionBuffer(1 / 30)
    old = Request(0, 1, 1, 0, 0, 0)
    assert buffer.integrate(old, rows(0.0, 0.0), 0, 0)
    for step in range(40):
        action, index, _ = buffer.current(step / 30)
        assert index == step
        assert action[0] == 0
    assert buffer.remaining() == 10
    new = Request(0, 2, 2, 40 / 30, 40 / 30, 10)
    for step in range(4):
        buffer.current((40 + step) / 30)
    assert buffer.remaining() == 6
    assert buffer.integrate(new, rows(1.0, 1.0), 40 / 30, 44 / 30)
    assert buffer.last_trimmed_steps == 4
    assert buffer.remaining() == 46  # old six replaced; new 46-step suffix
    first, index, _ = buffer.current(44 / 30)
    assert index == 4
    assert first[0] == pytest.approx(1 / 7)
    assert first[6] == pytest.approx(1 / 7)  # OpenArm blends grippers too
    for step in range(5):
        buffer.current((45 + step) / 30)
    seventh, index, _ = buffer.current(50 / 30)
    assert index == 10
    assert seventh[0] == pytest.approx(1)


def test_tda_no_overlap_appends_and_request_during_resume_does_not_consume():
    q = np.zeros(14)
    arbiter = Arbiter(
        Mode.INFERENCE, streaming=True, action_dt=1 / 30,
        policy_fusion="tda_smooth", max_action_age=3,
    )
    arbiter.start(q)
    token = arbiter.request(1, 1.0, observed_at=1.0)
    assert token.queue_size_at_request == 0
    assert arbiter.accept(token, rows(0.02), 1.03)
    assert arbiter.action_buffer.remaining() == 50
    decision = arbiter.step(q, q, now=1.04, dt=1 / 30, leader_ready=False)
    assert decision.phase == Phase.RESUME
    assert arbiter.action_buffer.remaining() == 50
    decision = arbiter.step(q, q, now=1.07, dt=1 / 30, leader_ready=True)
    assert decision.source == "policy"
    assert arbiter.action_buffer.remaining() == 49
