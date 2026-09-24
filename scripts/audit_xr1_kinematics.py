"""Read-only FK / XR-1 delta / IK check on recorded YAM HDF5 samples.

Usage: python scripts/audit_xr1_kinematics.py /path/to/episode_000001
Images are not loaded.  A failed or multi-solution IK is reported, never
silently replaced with the original recorded joints.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import h5py
import numpy as np

from yam_abc_reproduce.hil.xr1_actions import XR1YamCodec


def audit(episode: Path, *, stride: int = 60, horizon: int = 30) -> dict:
    manifest = json.loads((episode / "manifest.json").read_text())
    codec = XR1YamCodec()
    result = {
        "episode": str(episode),
        "recorded_steps": manifest["steps"],
        "segments": len(manifest["segments"]),
        "windows": 0,
        "converted": 0,
        "failed": 0,
        "failure_examples": [],
        "out_of_model_limit_target_rows": 0,
        "max_target_limit_excess_rad": 0.0,
        "max_position_error_m": 0.0,
        "max_orientation_error_rad": 0.0,
        "max_joint_difference_rad": 0.0,
        "max_gripper_difference": 0.0,
    }
    for segment in manifest["segments"]:
        with h5py.File(episode / segment["path"] / "samples.h5", "r") as samples:
            total = int(samples["committed_rows"][()])
            limits = codec.kinematics.left.joint_limits
            arm_joints = samples["submitted_action"][
                :total, [0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12]
            ]
            lower = np.tile(limits[:, 0], 2)
            upper = np.tile(limits[:, 1], 2)
            excess = np.maximum(lower - arm_joints, arm_joints - upper).clip(min=0).max(axis=1)
            valid = samples["submitted_action__valid"][:total].astype(bool)
            result["out_of_model_limit_target_rows"] += int(((excess > 1e-5) & valid).sum())
            result["max_target_limit_excess_rad"] = max(
                result["max_target_limit_excess_rad"],
                float(excess[valid].max(initial=0)),
            )
            for start in range(0, total - horizon + 1, stride):
                if not samples["observation_state__valid"][start]:
                    continue
                if not samples["submitted_action__valid"][start : start + horizon].all():
                    continue
                observed = samples["observation_state"][start]
                targets = samples["submitted_action"][start : start + horizon]
                result["windows"] += 1
                try:
                    relative = codec.encode(observed, targets)
                    recovered = codec.decode(observed, relative)
                    expected_poses, _ = codec.kinematics.forward_batch(targets)
                    actual_poses, _ = codec.kinematics.forward_batch(recovered)
                    position = np.linalg.norm(
                        expected_poses[:, :, :3, 3] - actual_poses[:, :, :3, 3],
                        axis=-1,
                    )
                    orientation = np.einsum(
                        "naij,najk->naik",
                        expected_poses[:, :, :3, :3].transpose(0, 1, 3, 2),
                        actual_poses[:, :, :3, :3],
                    )
                    angle = np.arccos(
                        np.clip(
                            (np.trace(orientation, axis1=-2, axis2=-1) - 1) / 2,
                            -1,
                            1,
                        )
                    )
                    joints = [0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12]
                    result["max_position_error_m"] = max(
                        result["max_position_error_m"],
                        float(position.max()),
                    )
                    result["max_orientation_error_rad"] = max(
                        result["max_orientation_error_rad"],
                        float(angle.max()),
                    )
                    result["max_joint_difference_rad"] = max(
                        result["max_joint_difference_rad"],
                        float(np.abs(targets[:, joints] - recovered[:, joints]).max()),
                    )
                    result["max_gripper_difference"] = max(
                        result["max_gripper_difference"],
                        float(np.abs(targets[:, [6, 13]] - recovered[:, [6, 13]]).max()),
                    )
                    result["converted"] += 1
                except (ValueError, RuntimeError) as exc:
                    result["failed"] += 1
                    if len(result["failure_examples"]) < 5:
                        result["failure_examples"].append(
                            {
                                "segment": segment["path"],
                                "start": start,
                                "error": str(exc),
                            }
                        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episode", type=Path)
    parser.add_argument("--stride", type=int, default=60)
    parser.add_argument("--horizon", type=int, default=30)
    args = parser.parse_args()
    if args.stride <= 0 or not 0 < args.horizon <= 30:
        parser.error("stride must be positive and horizon must be 1–30")
    print(json.dumps(audit(args.episode, stride=args.stride, horizon=args.horizon), indent=2))


if __name__ == "__main__":
    main()
