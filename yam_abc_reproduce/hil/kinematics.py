"""YAM follower joint/EEF conversion shared by policy decoding and data export.

The canonical pose is the official ``linear_4310/grasp_site`` frame in each
arm's own base frame.  It is a 4x4 SE(3) matrix in metres.  This module has no
SDK or CAN dependency; callers decide the wire representation and timing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import mink
import numpy as np
from i2rt.robots.kinematics import Kinematics
from i2rt.robots.utils import ArmType, GripperType, combine_arm_and_gripper_xml


@dataclass(frozen=True)
class EefState:
    left: np.ndarray
    left_gripper: float
    right: np.ndarray
    right_gripper: float


def _vector(value, size: int, name: str) -> np.ndarray:
    result = np.asarray(value, dtype=np.float64)
    if result.shape != (size,) or not np.isfinite(result).all():
        raise ValueError(f"{name} must be finite ({size},)")
    return result


def _pose(value, name: str) -> np.ndarray:
    result = np.asarray(value, dtype=np.float64)
    if result.shape != (4, 4) or not np.isfinite(result).all():
        raise ValueError(f"{name} must be finite (4,4)")
    rotation = result[:3, :3]
    if (
        not np.allclose(result[3], [0, 0, 0, 1], atol=1e-8)
        or not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-5)
        or not np.isclose(np.linalg.det(rotation), 1, atol=1e-5)
    ):
        raise ValueError(f"{name} must be a rigid SE(3) transform")
    return result


def _model(gripper: GripperType) -> Kinematics:
    path = Path(combine_arm_and_gripper_xml(ArmType.YAM, gripper))
    try:
        return Kinematics(str(path), "grasp_site")
    finally:
        path.unlink(missing_ok=True)


class YamEefKinematics:
    """Official YAM FK/IK with the standard follower gripper's tool offset.

    The official 6-DOF IK solves the flange pose.  A fixed transform from that
    flange to the installed gripper grasp site maps it to the actual TCP.  One
    instance owns mutable Mink state and must not be shared across threads.
    """

    def __init__(self):
        self._arm = _model(GripperType.NO_GRIPPER)
        gripper = _model(GripperType.LINEAR_4310)
        zero = np.zeros(6)
        flange = self._arm.fk(zero)
        grasp = gripper.fk(np.zeros(gripper._configuration.model.nq))
        self._flange_to_grasp = np.linalg.inv(flange) @ grasp
        self.joint_limits = self._arm._configuration.model.jnt_range[:6].copy()
        self._limits = [mink.ConfigurationLimit(self._arm._configuration.model)]

    def fk(self, joints) -> np.ndarray:
        q = _vector(joints, 6, "joints")
        return self._arm.fk(q) @ self._flange_to_grasp

    def ik(self, grasp_pose, seed) -> np.ndarray:
        """Solve near a measured/previous q; reject unreachable or invalid poses."""
        target = _pose(grasp_pose, "grasp_pose")
        initial = _vector(seed, 6, "seed")
        lower, upper = self.joint_limits[:, 0], self.joint_limits[:, 1]
        if np.any(initial < lower - 1e-5) or np.any(initial > upper + 1e-5):
            raise ValueError("IK seed outside official joint limits")
        # Already there is common when converting recordings.  It also avoids
        # an unnecessary QP solve at the boundary of an official joint limit.
        if self._within_tolerance(self.fk(initial), target):
            return initial.copy()
        flange_target = target @ np.linalg.inv(self._flange_to_grasp)
        success, solution = self._arm.ik(
            flange_target,
            "grasp_site",
            init_q=initial,
            limits=self._limits,
        )
        q = np.asarray(solution, dtype=np.float64)[:6].copy()
        if not np.isfinite(q).all():
            raise ValueError("IK returned nonfinite YAM joints")
        pose_error = self.fk(q)
        if (
            not success
            or np.any(q < lower - 1e-5)
            or np.any(q > upper + 1e-5)
            or not self._within_tolerance(pose_error, target, tolerance=2e-4)
        ):
            position = np.linalg.norm(pose_error[:3, 3] - target[:3, 3])
            rotation = pose_error[:3, :3].T @ target[:3, :3]
            angle = np.arccos(np.clip((np.trace(rotation) - 1) / 2, -1, 1))
            raise ValueError(
                "IK did not reach a valid YAM grasp pose "
                f"(success={success}, position_error_m={position:.6g}, "
                f"orientation_error_rad={angle:.6g})"
            )
        return q

    @staticmethod
    def _within_tolerance(actual: np.ndarray, target: np.ndarray, tolerance=1e-4) -> bool:
        rotation_error = actual[:3, :3].T @ target[:3, :3]
        angle = np.arccos(np.clip((np.trace(rotation_error) - 1) / 2, -1, 1))
        return np.linalg.norm(actual[:3, 3] - target[:3, 3]) <= tolerance and angle <= tolerance


class DualArmEefConverter:
    """Convert the existing 14D absolute contract to/from two TCP poses."""

    def __init__(self):
        self.left = YamEefKinematics()
        self.right = YamEefKinematics()

    def forward(self, joint_state) -> EefState:
        state = _vector(joint_state, 14, "joint_state")
        return EefState(
            self.left.fk(state[:6]),
            float(state[6]),
            self.right.fk(state[7:13]),
            float(state[13]),
        )

    def inverse(self, eef: EefState, seed) -> np.ndarray:
        previous = _vector(seed, 14, "seed")
        grippers = _vector([eef.left_gripper, eef.right_gripper], 2, "grippers")
        return np.concatenate(
            (
                self.left.ik(eef.left, previous[:6]),
                grippers[:1],
                self.right.ik(eef.right, previous[7:13]),
                grippers[1:],
            )
        )

    def forward_batch(self, joint_states) -> tuple[np.ndarray, np.ndarray]:
        """Dataset export: (N,14) -> (N,2,4,4) TCPs and (N,2) grippers."""
        rows = np.asarray(joint_states, dtype=np.float64)
        if rows.ndim != 2 or rows.shape[1] != 14 or not np.isfinite(rows).all():
            raise ValueError("joint_states must be finite (N,14)")
        poses = np.empty((len(rows), 2, 4, 4), dtype=np.float64)
        for i, row in enumerate(rows):
            poses[i, 0] = self.left.fk(row[:6])
            poses[i, 1] = self.right.fk(row[7:13])
        return poses, rows[:, [6, 13]].copy()

    def inverse_batch(self, poses, grippers, initial_state) -> np.ndarray:
        """Policy decoder: preserve continuity by seeding each IK from the last."""
        transforms = np.asarray(poses, dtype=np.float64)
        grip = np.asarray(grippers, dtype=np.float64)
        if transforms.ndim != 4 or transforms.shape[1:] != (2, 4, 4):
            raise ValueError("poses must have shape (N,2,4,4)")
        if grip.shape != (len(transforms), 2) or not np.isfinite(grip).all():
            raise ValueError("grippers must be finite (N,2)")
        previous = _vector(initial_state, 14, "initial_state")
        actions = np.empty((len(transforms), 14), dtype=np.float64)
        for i, (pose, fingers) in enumerate(zip(transforms, grip, strict=True)):
            try:
                previous = self.inverse(
                    EefState(pose[0], fingers[0], pose[1], fingers[1]), previous
                )
            except ValueError as exc:
                raise ValueError(f"IK failed at action step {i}: {exc}") from exc
            actions[i] = previous
        return actions
