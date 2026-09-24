"""XR-1's native relative EEF action contract for YAM offline conversion.

This accepts *denormalized* (N,60) actions.  XR-1 training and inference both
express each target relative to the same observation TCP, not relative to the
previous action in the chunk.  Network protocol and action scheduling are
separate from this codec; it never writes a motor target itself.
"""

from __future__ import annotations

import numpy as np
from scipy.spatial.transform import Rotation

from .kinematics import DualArmEefConverter

ACTION_DIM = 60
ACTION_HORIZON = 30
ACTIVE_SLOTS = (0, 1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 13, 14)


def action_mask(length: int) -> np.ndarray:
    """YAM has no XR-1 seventh joints, waist, or mobile base action."""
    if not 0 <= length <= ACTION_HORIZON:
        raise ValueError("XR-1 valid action length must be 0–30")
    mask = np.zeros((ACTION_HORIZON, ACTION_DIM), dtype=np.int32)
    mask[:length, ACTIVE_SLOTS] = 1
    return mask


def _actions(value) -> np.ndarray:
    rows = np.asarray(value, dtype=np.float64)
    if (
        rows.ndim != 2
        or rows.shape[1] != ACTION_DIM
        or not 1 <= len(rows) <= ACTION_HORIZON
        or not np.isfinite(rows).all()
    ):
        raise ValueError("XR-1 actions must be finite (N,60) denormalized deltas, N=1–30")
    return rows


class XR1YamCodec:
    """Use one YAM FK/IK source for dataset conversion and policy decoding."""

    def __init__(self):
        self.kinematics = DualArmEefConverter()

    @staticmethod
    def state60(joint_state) -> np.ndarray:
        """XR-1 native proprio layout; seventh joint and unused slots are zero."""
        q = np.asarray(joint_state, dtype=np.float64)
        if q.shape != (14,) or not np.isfinite(q).all():
            raise ValueError("joint_state must be finite (14,)")
        state = np.zeros((1, 60), dtype=np.float32)
        state[0, :6], state[0, 7] = q[:6], q[6]
        state[0, 8:14], state[0, 15] = q[7:13], q[13]
        return state

    def robot_state(self, joint_state) -> dict[str, np.ndarray]:
        """Input to Xiaomi's native ``recover_action`` / policy client."""
        q = np.asarray(joint_state, dtype=np.float64)
        if q.shape != (14,) or not np.isfinite(q).all():
            raise ValueError("joint_state must be finite (14,)")
        eef = self.kinematics.forward(q)
        return {
            "left_arm_joint": q[:6].copy(),
            "left_gripper_pos": np.array([q[6]]),
            "left_ee_pos": eef.left[:3, 3].copy(),
            "left_ee_rotm": eef.left[:3, :3].copy(),
            "right_arm_joint": q[7:13].copy(),
            "right_gripper_pos": np.array([q[13]]),
            "right_ee_pos": eef.right[:3, 3].copy(),
            "right_ee_rotm": eef.right[:3, :3].copy(),
        }

    def decode_targets(self, observation_state, targets: dict) -> np.ndarray:
        """Decode Xiaomi's already recovered absolute EEF ``action_targets``."""
        positions = []
        rotations = []
        grippers = []
        for side in ("left", "right"):
            pos = np.asarray(targets[f"{side}_ee_pos"], dtype=np.float64)
            rot = np.asarray(targets[f"{side}_ee_rotm"], dtype=np.float64)
            grip = np.asarray(targets[f"{side}_gripper_pos"], dtype=np.float64)
            if pos.ndim != 2 or pos.shape[1] != 3 or rot.shape != (len(pos), 3, 3):
                raise ValueError(f"{side} XR-1 target poses have invalid shape")
            if grip.shape not in ((len(pos),), (len(pos), 1)):
                raise ValueError(f"{side} XR-1 gripper targets have invalid shape")
            positions.append(pos)
            rotations.append(rot)
            grippers.append(grip.reshape(-1))
        if (
            not 1 <= len(positions[0]) <= ACTION_HORIZON
            or len(positions[0]) != len(positions[1])
            or not all(np.isfinite(value).all() for value in (*positions, *rotations, *grippers))
        ):
            raise ValueError("XR-1 targets must have matching length 1–30 and finite values")
        poses = np.broadcast_to(np.eye(4), (len(positions[0]), 2, 4, 4)).copy()
        for index in range(2):
            poses[:, index, :3, 3] = positions[index]
            poses[:, index, :3, :3] = rotations[index]
        return self.kinematics.inverse_batch(poses, np.column_stack(grippers), observation_state)

    def encode(self, observation_state, target_actions) -> np.ndarray:
        """Dataset cleaning: absolute 14D targets -> XR-1 local EEF deltas.

        The caller supplies targets paired to an observation row.  Do not use
        measured feedback in place of recorded submitted/expert actions.
        """
        observation = self.kinematics.forward(observation_state)
        target_poses, target_grippers = self.kinematics.forward_batch(target_actions)
        if not 1 <= len(target_poses) <= ACTION_HORIZON:
            raise ValueError("XR-1 target action length must be 1–30")
        raw = np.zeros((len(target_poses), ACTION_DIM), dtype=np.float32)
        for side, pose, grip, offset in (
            (observation.left, target_poses[:, 0], target_grippers[:, 0], 0),
            (observation.right, target_poses[:, 1], target_grippers[:, 1], 8),
        ):
            rotation = side[:3, :3]
            raw[:, offset : offset + 3] = (pose[:, :3, 3] - side[:3, 3]) @ rotation
            relative = np.einsum("ij,njk->nik", rotation.T, pose[:, :3, :3])
            raw[:, offset + 3 : offset + 6] = Rotation.from_matrix(relative).as_rotvec()
            current_grip = observation.left_gripper if offset == 0 else observation.right_gripper
            raw[:, offset + 6] = grip - current_grip
        return raw

    def decode(self, observation_state, action_deltas) -> np.ndarray:
        """Inference: XR-1 local deltas -> absolute YAM 14D joint targets.

        Each IK uses the previous solution as its seed, while every target pose
        and gripper value is recovered from the *same* observation.  This
        matches XR-1's ``recover_action`` and avoids accumulating deltas.
        """
        raw = _actions(action_deltas)
        observation = self.kinematics.forward(observation_state)
        poses = np.empty((len(raw), 2, 4, 4), dtype=np.float64)
        grippers = np.empty((len(raw), 2), dtype=np.float64)
        for index, base, offset in (
            (0, observation.left, 0),
            (1, observation.right, 8),
        ):
            rotation = base[:3, :3]
            poses[:, index] = base
            poses[:, index, :3, 3] = base[:3, 3] + raw[:, offset : offset + 3] @ rotation.T
            delta_rotation = Rotation.from_rotvec(raw[:, offset + 3 : offset + 6]).as_matrix()
            poses[:, index, :3, :3] = np.einsum("ij,njk->nik", rotation, delta_rotation)
            current_grip = observation.left_gripper if index == 0 else observation.right_gripper
            grippers[:, index] = current_grip + raw[:, offset + 6]
        return self.kinematics.inverse_batch(poses, grippers, observation_state)
