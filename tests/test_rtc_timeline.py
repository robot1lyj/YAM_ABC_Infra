"""Trained RTC tick/prefix ownership without Thor or a robot."""

import numpy as np
import pytest

from yam_abc_reproduce.hil.rtc_timeline import RtcTimeline


def action(value):
    out = np.zeros(14)
    out[0] = value
    return out


def limits(value):
    out = np.asarray(value).copy()
    out[0] = np.clip(out[0], -1, 1)
    return out


def test_prefix_uses_actual_history_and_locks_future_at_original_ticks():
    clock = RtcTimeline(delay_steps=8)
    for tick in range(100, 103):
        clock.record_submitted(tick, action(tick / 1000))
    request = clock.prepare(observation_tick=101, current_tick=102, limit_target=limits)
    assert request.takeover_tick == 109
    np.testing.assert_allclose(request.actions[:2, 0], [.101, .102])
    assert np.all(request.actions[2:, 0] == pytest.approx(.102))
    rows = np.vstack([action(.102) for _ in range(50)])
    rows[:8] = request.actions
    rows[8:, 0] = .8
    assert clock.install(request, rows, current_tick=107, limit_target=limits)
    for tick in range(103, 109):
        selected, source = clock.select(tick, action(0))
        assert source == "hold"
        assert selected[0] == pytest.approx(.102)
        clock.record_submitted(tick, selected)
    selected, source = clock.select(109, action(0))
    assert source == "policy" and selected[0] == pytest.approx(.8)


def test_late_reply_is_dropped_without_retiming():
    clock = RtcTimeline(delay_steps=8)
    for tick in range(3):
        clock.record_submitted(tick, action(.1))
    request = clock.prepare(observation_tick=1, current_tick=2, limit_target=limits)
    rows = np.vstack([action(.1) for _ in range(50)])
    rows[9:, 0] = .9
    assert not clock.install(request, rows, current_tick=10, limit_target=limits)
    selected, source = clock.select(9, action(.1))
    assert source == "hold" and selected[0] == pytest.approx(.1)


def test_reset_rejects_old_reply_and_committed_change_fails():
    clock = RtcTimeline(delay_steps=8)
    clock.record_submitted(0, action(.1))
    request = clock.prepare(observation_tick=0, current_tick=0, limit_target=limits)
    with pytest.raises(RuntimeError, match="committed target changed"):
        clock.record_submitted(1, action(.4))
    assert clock.pending is None
    assert not clock.install(request, np.zeros((50, 14)), current_tick=1, limit_target=limits)


def test_joint_limit_is_applied_to_future_and_new_plan():
    clock = RtcTimeline(delay_steps=8)
    clock.record_submitted(0, action(.1))
    clock._plan[1] = action(3)
    request = clock.prepare(observation_tick=0, current_tick=0, limit_target=limits)
    assert request.actions[1, 0] == pytest.approx(1)
    rows = np.vstack([action(3) for _ in range(50)])
    rows[:8] = request.actions
    assert clock.install(request, rows, current_tick=2, limit_target=limits)
    assert clock.select(8, action(0))[0][0] == pytest.approx(1)


def test_finite_out_of_range_gripper_is_clipped_before_strict_sdk_target():
    from yam_abc_reproduce.hil.core import vector

    clock = RtcTimeline(delay_steps=8)
    clock.record_submitted(0, action(.1))
    request = clock.prepare(observation_tick=0, current_tick=0,
                            limit_target=vector)
    rows = np.vstack([action(.1) for _ in range(50)])
    rows[:8] = request.actions
    rows[8:, 6] = -0.2
    rows[8:, 13] = 1.2
    assert clock.install(request, rows, current_tick=2, limit_target=vector)
    selected, _ = clock.select(8, action(0))
    assert selected[6] == 0 and selected[13] == 1


def test_missing_history_cannot_be_fabricated():
    clock = RtcTimeline(delay_steps=8)
    clock.record_submitted(5, action(0))
    with pytest.raises(ValueError, match="history has a gap"):
        clock.prepare(observation_tick=4, current_tick=5, limit_target=limits)
