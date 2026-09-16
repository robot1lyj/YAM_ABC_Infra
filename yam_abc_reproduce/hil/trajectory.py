"""Policy-only high-rate trajectory execution under the 30 Hz arbiter.

The filters have no hardware access. TrajectoryExecutor owns one writer thread;
StationIO joins it before HOLD, manual or maintenance writes resume.
"""

from __future__ import annotations

import threading
import time
from collections import deque

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
        max_gripper_speed: float | None = 1.0,
    ):
        values = {
            "max_joint_speed": max_joint_speed,
            "max_joint_acceleration": max_joint_acceleration,
            "natural_frequency": natural_frequency,
        }
        if any(not np.isfinite(value) or value <= 0 for value in values.values()):
            raise ValueError("trajectory limits must be finite and positive")
        if max_gripper_speed is not None and (
            not np.isfinite(max_gripper_speed) or max_gripper_speed <= 0
        ):
            raise ValueError("trajectory limits must be finite and positive")
        self.max_joint_speed = float(max_joint_speed)
        self.max_joint_acceleration = float(max_joint_acceleration)
        self.natural_frequency = float(natural_frequency)
        self.max_gripper_speed = None if max_gripper_speed is None else float(max_gripper_speed)
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
            delta = target[index] - self.position[index]
            if self.max_gripper_speed is not None:
                delta = np.clip(
                    delta,
                    -self.max_gripper_speed * dt,
                    self.max_gripper_speed * dt,
                )
            self.position[index] += delta
            self.velocity[index] = delta / dt
        return self.position.copy()


class LinearTrajectoryInterpolator:
    """Causal 30 Hz target-to-100 Hz line segments without second-order lag."""

    def __init__(self, initial, *, duration_s: float, max_joint_speed: float):
        if not np.isfinite(duration_s) or duration_s <= 0:
            raise ValueError("interpolation duration must be finite and positive")
        if not np.isfinite(max_joint_speed) or max_joint_speed <= 0:
            raise ValueError("interpolation joint speed must be finite and positive")
        self.duration_s = float(duration_s)
        self.max_joint_speed = float(max_joint_speed)
        self.position = vector(initial).copy()
        self.velocity = np.zeros(14)
        self.start = self.position.copy()
        self.target = self.position.copy()
        self.elapsed_s = 0.0

    def reset(self, position):
        self.position = vector(position).copy()
        self.start = self.position.copy()
        self.target = self.position.copy()
        self.elapsed_s = 0.0
        self.velocity.fill(0)
        return self.position.copy()

    def set_target(self, target):
        self.start = self.position.copy()
        self.target = vector(target).copy()
        self.elapsed_s = 0.0

    def step(self, dt: float):
        if not np.isfinite(dt) or dt <= 0:
            raise ValueError("trajectory dt must be finite and positive")
        self.elapsed_s += dt
        alpha = min(1.0, self.elapsed_s / self.duration_s)
        desired = self.start + alpha * (self.target - self.start)
        previous = self.position.copy()
        joints = list(JOINTS)
        delta = np.clip(
            desired[joints] - previous[joints],
            -self.max_joint_speed * dt,
            self.max_joint_speed * dt,
        )
        self.position[joints] += delta
        self.position[list(GRIPPERS)] = self.target[list(GRIPPERS)]
        self.velocity = (self.position - previous) / dt
        return self.position.copy()


class TrajectoryExecutor:
    """Latest-target-wins high-rate sampler with exactly one write thread."""

    def __init__(
        self,
        initial,
        write,
        *,
        hz: float = 100.0,
        max_joint_speed: float = 3.0,
        max_joint_acceleration: float = 30.0,
        natural_frequency: float = 10.0,
        mode: str = "second_order",
        action_dt_s: float = 1 / 30,
        watchdog_s: float = 0.15,
    ):
        if not np.isfinite(hz) or hz <= 0 or not np.isfinite(watchdog_s) or watchdog_s <= 0:
            raise ValueError("trajectory executor timing must be finite and positive")
        if mode not in ("second_order", "linear"):
            raise ValueError("trajectory mode must be second_order or linear")
        self.period = 1.0 / float(hz)
        self.watchdog_s = float(watchdog_s)
        self.write = write
        self.mode = mode
        self.filter = (
            LinearTrajectoryInterpolator(
                initial, duration_s=action_dt_s, max_joint_speed=max_joint_speed
            )
            if mode == "linear"
            else TrajectoryFilter(
                initial,
                max_joint_speed=max_joint_speed,
                max_joint_acceleration=max_joint_acceleration,
                natural_frequency=natural_frequency,
                max_gripper_speed=None,
            )
        )
        self._condition = threading.Condition()
        self._target = vector(initial).copy()
        self._latest = self._target.copy()
        self._updated_at = time.monotonic()
        self._error = None
        self._trace = deque(maxlen=64)
        self._trace_seq = 0
        self._trace_lost = 0
        self._stopping = False
        self._thread = threading.Thread(
            target=self._run,
            name="policy-trajectory",
            daemon=True,
        )
        self._thread.start()

    def submit(self, target):
        target = vector(target).copy()
        with self._condition:
            self._raise_if_failed()
            self._target = target
            self._updated_at = time.monotonic()
            self._condition.notify()

    def latest(self):
        with self._condition:
            self._raise_if_failed()
            return self._latest.copy()

    def drain_trace(self):
        """Bounded RAM handoff to the 30 Hz recorder; never write storage here."""
        with self._condition:
            rows = list(self._trace)
            self._trace.clear()
            lost = self._trace_lost
            self._trace_lost = 0
            return {"samples": rows, "lost": lost}

    def close(self):
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        self._thread.join(timeout=max(1.0, self.period * 10))
        if self._thread.is_alive():
            raise RuntimeError("trajectory executor did not stop")
        with self._condition:
            self._raise_if_failed()

    def _raise_if_failed(self):
        if self._error is not None:
            raise RuntimeError(f"trajectory executor failed: {self._error}")

    def _run(self):
        deadline = time.monotonic()
        stale = False
        last_target_update = None
        try:
            while True:
                with self._condition:
                    if self._stopping:
                        return
                    target = self._target.copy()
                    updated_at = self._updated_at
                    target_age = time.monotonic() - updated_at
                if target_age > self.watchdog_s:
                    if not stale:
                        self.filter.reset(self._latest)
                        stale = True
                    target = self._latest
                else:
                    stale = False
                if self.mode == "linear":
                    if updated_at != last_target_update or stale:
                        self.filter.set_target(target)
                        last_target_update = updated_at
                    filtered = self.filter.step(self.period)
                else:
                    filtered = self.filter.step(target, self.period)
                velocity = self.filter.velocity.copy()
                started_at = time.monotonic()
                result = self.write(filtered)
                completed_at = time.monotonic()
                submitted, arm_stamps = result if isinstance(result, tuple) else (result, None)
                submitted = vector(submitted)
                self.filter.position = submitted.copy()
                with self._condition:
                    self._latest = submitted.copy()
                    if len(self._trace) == self._trace.maxlen:
                        self._trace_lost += 1
                    self._trace_seq += 1
                    self._trace.append({
                        "seq": self._trace_seq,
                        "target_updated_at": updated_at,
                        "write_started_at": started_at,
                        "write_completed_at": completed_at,
                        "target": target.tolist(),
                        "filtered": filtered.tolist(),
                        "velocity": velocity.tolist(),
                        "submitted": submitted.tolist(),
                        "arms": arm_stamps,
                    })
                deadline += self.period
                remaining = deadline - time.monotonic()
                if remaining < 0:
                    deadline = time.monotonic()
                else:
                    with self._condition:
                        self._condition.wait_for(lambda: self._stopping, timeout=remaining)
        except Exception as exc:
            with self._condition:
                self._error = exc
                self._stopping = True
                self._condition.notify_all()
