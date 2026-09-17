"""Four-arm IO adapter, with all motor writes owned by the runtime tick."""

import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from ..resource_qos import place_on_cpus
from .core import vector
from .trajectory import TrajectoryExecutor


class StationIO:
    def __init__(
        self,
        units,
        *,
        mock=False,
        leader_gain=0.2,
        leader_speed=0.5,
        policy_trajectory_hz=0,
        policy_joint_speed=3.0,
        policy_joint_acceleration=30.0,
        policy_natural_frequency=10.0,
    ):
        by_name = {u.name: u for u in units}
        if set(by_name) != {"left", "right"} or len(units) != 2:
            raise ValueError("exactly left and right YAM followers required")
        self.units = [by_name["left"], by_name["right"]]
        self.mock = mock
        # Every real arm has its own CAN interface and i2rt state lock. Waiting for
        # those four independent snapshots serially adds their lock-contention times
        # together and made a nominal 30 Hz tick take 36-77 ms on RK3588. Reads are
        # side-effect free, so issue them together; writes remain below in one ordered
        # control-owner thread.
        self._read_pool = (
            None
            if mock
            else ThreadPoolExecutor(
                max_workers=4,
                thread_name_prefix="station-read",
                initializer=place_on_cpus,
                initargs=("CONTROL",),
            )
        )
        self.read_timings_s = {}
        self.leader_gain, self.leader_speed = leader_gain, leader_speed
        self.policy_trajectory_hz = float(policy_trajectory_hz or 0)
        self.policy_joint_speed = float(policy_joint_speed)
        self.policy_joint_acceleration = float(policy_joint_acceleration)
        self.policy_natural_frequency = float(policy_natural_frequency)
        self._policy_trajectory = None
        self._policy_trace = None
        self._mock_leaders = np.concatenate([u.robot.get_joint_pos() for u in self.units])
        self._manual = True
        self._mock_t = 0
        self._limits = [
            np.tile([-np.pi, np.pi], (6, 1)) if mock else u.robot.joint_limits() for u in self.units
        ]
        if any(
            limits.shape != (6, 2)
            or not np.isfinite(limits).all()
            or np.any(limits[:, 0] >= limits[:, 1])
            for limits in self._limits
        ):
            raise ValueError("invalid SDK joint limits")

    def read(self):
        if self.mock:
            q = vector(np.concatenate([u.robot.get_joint_pos() for u in self.units]))
            if self._manual:
                self._mock_t += 1
                self._mock_leaders[[0, 7]] += 0.001 * np.cos(self._mock_t / 30)
            return q, self._mock_leaders.copy(), [[False, False], [False, False]], [0.0] * 4

        def read_follower(unit):
            started = time.monotonic()
            read = getattr(unit.robot, "hil_read", None)
            if callable(read):
                pos, age = read()
            else:
                pos = unit.robot.get_joint_pos()
                age = unit.robot.feedback_age()
            return pos, age, time.monotonic() - started

        def read_leader(unit):
            started = time.monotonic()
            pos, keys, age = unit.agent.hil_read()
            return pos, keys, age, time.monotonic() - started

        started = time.monotonic()
        follower_futures = [self._read_pool.submit(read_follower, unit) for unit in self.units]
        leader_futures = [self._read_pool.submit(read_leader, unit) for unit in self.units]
        followers = [future.result() for future in follower_futures]
        leaders_read = [future.result() for future in leader_futures]
        self.read_timings_s = {
            **{
                f"{unit.name}_follower": result[2]
                for unit, result in zip(self.units, followers, strict=True)
            },
            **{
                f"{unit.name}_leader": result[3]
                for unit, result in zip(self.units, leaders_read, strict=True)
            },
            "parallel_total": time.monotonic() - started,
        }
        q = vector(np.concatenate([result[0] for result in followers]))
        leaders = [result[0] for result in leaders_read]
        buttons = [[bool(key) for key in result[1][:2]] for result in leaders_read]
        ages = [
            age
            for pair in zip(
                [result[1] for result in followers],
                [result[2] for result in leaders_read],
                strict=True,
            )
            for age in pair
        ]
        return q, vector(np.concatenate(leaders)), buttons, ages

    def limit_policy_target(self, action):
        """The same hard-limit transform used for RTC commitments and SDK writes."""
        target = vector(action)
        for i, limits in enumerate(self._limits):
            sl = slice(i * 7, i * 7 + 6)
            target[sl] = np.clip(target[sl], limits[:, 0], limits[:, 1])
        return target

    def apply(
        self, decision, q, leader, *, dt, mirror=True, maintenance_leader=None, gravity=False
    ):
        target = self.limit_policy_target(decision.action)
        self._policy_trace = None
        policy_trajectory = (
            self.policy_trajectory_hz > 0 and decision.source == "policy" and not gravity
        )
        if not policy_trajectory and self._policy_trajectory is not None:
            self._policy_trajectory.close()
            self._policy_trace = self._policy_trajectory.drain_trace()
            self._policy_trajectory = None
        manual = (decision.leader_manual or not mirror) and maintenance_leader is None
        leader_targets = []
        for i, limits in enumerate(self._limits):
            sl = slice(i * 7, i * 7 + 6)
            # Mirror actual follower pose, including limited catch-up on RESUME.
            arm = np.clip(
                q[sl], leader[sl] - self.leader_speed * dt, leader[sl] + self.leader_speed * dt
            )
            if decision.leader_freeze:
                arm = leader[sl]
            if maintenance_leader is not None:
                arm = np.clip(
                    maintenance_leader[sl], leader[sl] - 0.12 * dt, leader[sl] + 0.12 * dt
                )
            leader_targets.append(np.clip(arm, limits[:, 0], limits[:, 1]))
        stamps = {}
        for i, u in enumerate(self.units):
            if self.mock:
                if not manual:
                    self._mock_leaders[i * 7 : i * 7 + 6] = leader_targets[i]
            else:
                u.agent.hil_leader_command(
                    leader_targets[i], manual=manual, gain_scale=self.leader_gain
                )
            stamps[f"{u.name}_leader"] = time.monotonic()
        self._manual = manual
        if policy_trajectory:
            if self._policy_trajectory is None:
                self._policy_trajectory = TrajectoryExecutor(
                    q,
                    lambda command: self._write_followers(command, timing=True),
                    hz=self.policy_trajectory_hz,
                    max_joint_speed=self.policy_joint_speed,
                    max_joint_acceleration=self.policy_joint_acceleration,
                    natural_frequency=self.policy_natural_frequency,
                )
            self._policy_trajectory.submit(target)
            submitted = self._policy_trajectory.latest()
            self._policy_trace = self._policy_trajectory.drain_trace()
            for u in self.units:
                stamps[f"{u.name}_follower"] = time.monotonic()
        else:
            submitted = self._write_followers(target, gravity=gravity)
            for u in self.units:
                stamps[f"{u.name}_follower"] = time.monotonic()
        return submitted, stamps

    def take_policy_trace(self):
        trace, self._policy_trace = self._policy_trace, None
        return trace

    def _write_followers(self, target, *, gravity=False, timing=False):
        target = vector(target).copy()
        arm_stamps = {} if timing else None
        for i, (u, limits) in enumerate(zip(self.units, self._limits, strict=True)):
            sl = slice(i * 7, i * 7 + 6)
            target[sl] = np.clip(target[sl], limits[:, 0], limits[:, 1])
            started_at = time.monotonic() if timing else None
            if gravity and not self.mock:
                u.robot.gravity_compensate(target[i * 7 : i * 7 + 7])
            elif not gravity:
                u.robot.command_joint_pos(target[i * 7 : i * 7 + 7])
            if timing:
                arm_stamps[u.name] = {
                    "sdk_call_started_at": started_at,
                    "sdk_call_returned_at": time.monotonic(),
                }
        return (target, arm_stamps) if timing else target

    def hold(self):
        errors = []
        if self._policy_trajectory is not None:
            try:
                self._policy_trajectory.close()
            except Exception as exc:
                errors.append(f"policy trajectory: {exc}")
            self._policy_trajectory = None
        for u in self.units:
            try:
                u.robot.command_joint_pos(u.robot.get_joint_pos())
            except Exception as exc:
                errors.append(f"{u.name} follower: {exc}")
            if not self.mock:
                try:
                    u.agent.hil_leader_command(np.zeros(6), manual=True)
                except Exception as exc:
                    errors.append(f"{u.name} leader: {exc}")
        return errors

    def close(self):
        if self._policy_trajectory is not None:
            self._policy_trajectory.close()
            self._policy_trajectory = None
        if self._read_pool is not None:
            self._read_pool.shutdown(wait=True, cancel_futures=True)
            self._read_pool = None
        self.hold()
        errors = []
        for u in self.units:
            for device in (u.agent, u.robot):
                try:
                    close = getattr(device, "close_hil", None)
                    if close:
                        close()
                    elif self.mock:
                        device.stop()
                except Exception as exc:
                    errors.append(str(exc))
        return errors
