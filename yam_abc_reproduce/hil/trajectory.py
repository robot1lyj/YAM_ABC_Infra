"""Experimental high-rate trajectory shaping, independent of motor IO.

The 30 Hz arbiter remains authoritative.  This module has no thread, queue or
hardware callback; callers explicitly sample it at their chosen higher rate.
That keeps offline replay representative while preventing an experimental
trajectory path from silently becoming a second motor writer.
"""

from __future__ import annotations

import numpy as np

from .core import vector

GRIPPERS = (6, 13)
JOINTS = tuple(index for index in range(14) if index not in GRIPPERS)


class TrajectoryFilter:
    """Critically damped, acceleration- and velocity-bounded target follower.

    Arm joints use a second-order critically damped response. Grippers remain
    independent and use a simple slew bound so joint filtering never averages
    or anticipates an open/close prediction.
    """

    def __init__(
        self,
        initial,
        *,
        max_joint_speed: float = 3.0,
        max_joint_acceleration: float = 30.0,
        natural_frequency: float = 10.0,
        max_gripper_speed: float = 1.0,
    ):
        values = {
            "max_joint_speed": max_joint_speed,
            "max_joint_acceleration": max_joint_acceleration,
            "natural_frequency": natural_frequency,
            "max_gripper_speed": max_gripper_speed,
        }
        if any(not np.isfinite(value) or value <= 0 for value in values.values()):
            raise ValueError("trajectory limits must be finite and positive")
        self.max_joint_speed = float(max_joint_speed)
        self.max_joint_acceleration = float(max_joint_acceleration)
        self.natural_frequency = float(natural_frequency)
        self.max_gripper_speed = float(max_gripper_speed)
        self.position = vector(initial).copy()
        self.velocity = np.zeros(14)

    def reset(self, position):
        """Cancel all prior motion, as required after HOLD/stop/epoch changes."""
        self.position = vector(position).copy()
        self.velocity.fill(0)
        return self.position.copy()

    def step(self, target, dt: float):
        if not np.isfinite(dt) or dt <= 0:
            raise ValueError("trajectory dt must be finite and positive")
        target = vector(target)
        joints = list(JOINTS)
        error = target[joints] - self.position[joints]
        omega = self.natural_frequency
        acceleration = omega * omega * error - 2.0 * omega * self.velocity[joints]
        acceleration = np.clip(
            acceleration,
            -self.max_joint_acceleration,
            self.max_joint_acceleration,
        )
        joint_velocity = np.clip(
            self.velocity[joints] + acceleration * dt,
            -self.max_joint_speed,
            self.max_joint_speed,
        )
        self.velocity[joints] = joint_velocity
        self.position[joints] += joint_velocity * dt

        for index in GRIPPERS:
            delta = np.clip(
                target[index] - self.position[index],
                -self.max_gripper_speed * dt,
                self.max_gripper_speed * dt,
            )
            self.position[index] += delta
            self.velocity[index] = delta / dt
        return self.position.copy()
