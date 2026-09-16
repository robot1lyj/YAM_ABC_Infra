import numpy as np
import pytest

from yam_abc_reproduce.hil.trajectory import JOINTS, TrajectoryFilter


def test_trajectory_bounds_joint_velocity_and_acceleration():
    follower = TrajectoryFilter(
        np.zeros(14),
        max_joint_speed=3.0,
        max_joint_acceleration=30.0,
        natural_frequency=10.0,
    )
    dt = 0.01
    target = np.ones(14)
    positions = np.array([follower.step(target, dt) for _ in range(100)])
    velocity = np.diff(np.vstack([np.zeros(14), positions]), axis=0) / dt
    acceleration = np.diff(np.vstack([np.zeros(14), velocity]), axis=0) / dt
    joints = list(JOINTS)
    assert np.max(np.abs(velocity[:, joints])) <= 3.0 + 1e-10
    assert np.max(np.abs(acceleration[:, joints])) <= 30.0 + 1e-9


def test_trajectory_reset_rejects_old_motion_and_gripper_is_independent():
    follower = TrajectoryFilter(np.zeros(14), max_gripper_speed=0.5)
    target = np.ones(14)
    for _ in range(10):
        follower.step(target, 0.01)
    held = follower.reset(np.full(14, 0.25))
    np.testing.assert_array_equal(held, np.full(14, 0.25))
    np.testing.assert_array_equal(follower.velocity, np.zeros(14))

    result = follower.step(np.r_[np.ones(6), 1.0, np.ones(6), 0.0], 0.01)
    assert result[6] == pytest.approx(0.255)
    assert result[13] == pytest.approx(0.245)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_joint_speed": 0},
        {"max_joint_acceleration": np.inf},
        {"natural_frequency": -1},
        {"max_gripper_speed": np.nan},
    ],
)
def test_trajectory_rejects_invalid_limits(kwargs):
    with pytest.raises(ValueError):
        TrajectoryFilter(np.zeros(14), **kwargs)
