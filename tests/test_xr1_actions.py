"""XR-1 EEF math must round-trip through the official YAM tool model."""

import numpy as np
import pytest
from i2rt.robots.kinematics import Kinematics
from i2rt.robots.utils import ArmType, GripperType, combine_arm_and_gripper_xml

from yam_abc_reproduce.hil.xr1_actions import XR1YamCodec, action_mask


@pytest.fixture(scope="module")
def codec():
    return XR1YamCodec()


@pytest.fixture
def state():
    return np.array([0.2, 1.1, 1.0, -0.3, 0.1, 0.2, 0.7, -0.1, 1.4, 0.8, 0.2, -0.1, 0.3, 0.3])


def test_fk_matches_official_linear_4310_grasp_site(codec, state):
    model = Kinematics(
        combine_arm_and_gripper_xml(ArmType.YAM, GripperType.LINEAR_4310),
        "grasp_site",
    )
    np.testing.assert_allclose(
        codec.kinematics.left.fk(state[:6]),
        model.fk(np.r_[state[:6], 0.0, 0.0]),
        atol=1e-8,
    )


def test_xr1_relative_contract_and_inverse(codec, state):
    targets = np.tile(state, (3, 1))
    targets[0, 0] += 0.005
    targets[1, 0] += 0.01
    targets[2, 0] += 0.02
    targets[:, 6] = [0.6, 0.4, 0.2]
    targets[:, 13] = [0.4, 0.5, 0.6]

    raw = codec.encode(state, targets)
    assert raw.shape == (3, 60)
    assert np.all(raw[:, 7] == 0) and np.all(raw[:, 15:] == 0)
    np.testing.assert_allclose(raw[:, 6], [-0.1, -0.3, -0.5], atol=1e-6)
    np.testing.assert_allclose(raw[:, 14], [0.1, 0.2, 0.3], atol=1e-6)

    restored = codec.decode(state, raw)
    actual, _ = codec.kinematics.forward_batch(restored)
    expected, _ = codec.kinematics.forward_batch(targets)
    np.testing.assert_allclose(actual[:, :, :3, 3], expected[:, :, :3, 3], atol=1e-4)
    np.testing.assert_allclose(actual[:, :, :3, :3], expected[:, :, :3, :3], atol=1e-4)
    np.testing.assert_allclose(restored[:, [6, 13]], targets[:, [6, 13]], atol=1e-6)
    assert np.all(np.diff(raw[:, 0]) > 0)  # all steps relative to one observation


def test_xr1_state_and_training_mask(codec, state):
    packed = codec.state60(state)
    physical = codec.robot_state(state)
    assert packed.shape == (1, 60)
    np.testing.assert_allclose(packed[0, :6], state[:6])
    np.testing.assert_allclose(packed[0, 8:14], state[7:13])
    assert packed[0, 6] == packed[0, 14] == 0
    np.testing.assert_allclose(physical["left_arm_joint"], state[:6])
    np.testing.assert_allclose(physical["right_arm_joint"], state[7:13])
    assert physical["left_ee_rotm"].shape == (3, 3)
    mask = action_mask(3)
    assert mask.dtype == np.int32
    assert mask.sum() == 42
    assert not mask[:, 7].any() and not mask[:, 16:].any()
    assert not mask[3:].any()


def test_decoded_absolute_xr1_targets_match_relative_codec(codec, state):
    deltas = np.zeros((2, 60))
    deltas[:, 0] = [0.003, 0.007]
    deltas[:, 6] = [-0.2, -0.3]
    deltas[:, 14] = [0.1, 0.2]
    base = codec.robot_state(state)
    targets = {}
    for side, offset in (("left", 0), ("right", 8)):
        rotation = base[f"{side}_ee_rotm"]
        targets[f"{side}_ee_pos"] = (
            base[f"{side}_ee_pos"] + deltas[:, offset : offset + 3] @ rotation.T
        )
        targets[f"{side}_ee_rotm"] = np.tile(rotation, (2, 1, 1))
        targets[f"{side}_gripper_pos"] = (
            base[f"{side}_gripper_pos"] + deltas[:, offset + 6 : offset + 7]
        )
    np.testing.assert_allclose(
        codec.decode_targets(state, targets),
        codec.decode(state, deltas),
        atol=1e-6,
    )


def test_invalid_or_unreachable_target_is_rejected(codec, state):
    with pytest.raises(ValueError, match="denormalized"):
        codec.decode(state, np.full((30, 60), np.nan))
    with pytest.raises(ValueError, match="N=1–30"):
        codec.decode(state, np.zeros((31, 60)))
    with pytest.raises(ValueError, match="N=1–30"):
        codec.decode(state, np.zeros((0, 60)))
    unreachable = np.zeros((1, 60))
    unreachable[0, 0] = 5.0
    with pytest.raises(ValueError, match="IK"):
        codec.decode(state, unreachable)
