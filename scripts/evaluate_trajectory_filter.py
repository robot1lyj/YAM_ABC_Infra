#!/usr/bin/env python3
"""Replay recorded 30 Hz selected targets through the experimental filter."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import h5py
import numpy as np

from yam_abc_reproduce.hil.trajectory import JOINTS, TrajectoryFilter


def percentile(values, points=(50, 95, 100)):
    return {
        f"p{point}": float(value)
        for point, value in zip(points, np.percentile(values, points), strict=True)
    }


def evaluate(path: Path, *, hz: float, speed: float, acceleration: float, frequency: float):
    with h5py.File(path, "r") as source:
        times = source["time"][:]
        targets = source["selected_action"][:]
        initial = source["submitted_action"][0]
    if len(times) < 2:
        raise ValueError("trajectory replay requires at least two recorded rows")

    dt = 1.0 / hz
    sample_times = np.arange(times[0], times[-1] + dt * 0.5, dt)
    follower = TrajectoryFilter(
        initial,
        max_joint_speed=speed,
        max_joint_acceleration=acceleration,
        natural_frequency=frequency,
    )
    positions, held_targets = [], []
    row = 0
    for now in sample_times:
        while row + 1 < len(times) and times[row + 1] <= now:
            row += 1
        held_targets.append(targets[row])
        positions.append(follower.step(targets[row], dt))

    positions = np.asarray(positions)
    held_targets = np.asarray(held_targets)
    joints = list(JOINTS)
    velocity = np.diff(positions[:, joints], axis=0) / dt
    joint_acceleration = np.diff(velocity, axis=0) / dt
    reversals = (
        (velocity[1:] * velocity[:-1] < 0)
        & (np.abs(velocity[1:]) > 0.15)
        & (np.abs(velocity[:-1]) > 0.15)
    )
    return {
        "path": str(path),
        "input_rows": len(times),
        "output_samples": len(sample_times),
        "trajectory_hz": hz,
        "max_joint_speed_rad_s": speed,
        "max_joint_acceleration_rad_s2": acceleration,
        "natural_frequency_rad_s": frequency,
        "direction_reversals_over_0_15_rad_s": int(np.sum(reversals)),
        "absolute_joint_velocity_rad_s": percentile(np.abs(velocity)),
        "absolute_joint_acceleration_rad_s2": percentile(np.abs(joint_acceleration)),
        "absolute_target_lag_rad": percentile(
            np.abs(held_targets[:, joints] - positions[:, joints])
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("samples_h5", type=Path)
    parser.add_argument("--hz", type=float, default=100.0)
    parser.add_argument("--max-joint-speed", type=float, default=3.0)
    parser.add_argument("--max-joint-acceleration", type=float, default=30.0)
    parser.add_argument("--natural-frequency", type=float, default=10.0)
    args = parser.parse_args()
    if any(
        value <= 0 or not np.isfinite(value)
        for value in (
            args.hz,
            args.max_joint_speed,
            args.max_joint_acceleration,
            args.natural_frequency,
        )
    ):
        parser.error("all trajectory parameters must be finite and positive")
    print(
        json.dumps(
            evaluate(
                args.samples_h5,
                hz=args.hz,
                speed=args.max_joint_speed,
                acceleration=args.max_joint_acceleration,
                frequency=args.natural_frequency,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
