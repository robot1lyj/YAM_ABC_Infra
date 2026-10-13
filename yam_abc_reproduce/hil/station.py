"""Follower IO with optional Leaders; all writes belong to the runtime tick."""

import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from ..resource_qos import place_on_cpus
from . import interaction_rules
from .cleanup import close_resources
from .core import vector
from .trajectory import TrajectoryExecutor


class StationIO:
    def device_health(self):
        arms = []
        for role in ("Follower", "Leader"):
            for unit in self.units:
                label = ("左" if unit.name == "left" else "右") + " " + role
                owner = unit.robot if role == "Follower" else unit.agent
                if owner is None:
                    continue
                if self.mock:
                    data = dict(running=True, feedback_age_s=0, motors=[
                        dict(joint=f"J{i+1}", id=i+1, error_code=1,
                             temp_mos_c=None, temp_rotor_c=None) for i in range(6)])
                else:
                    data = owner.hil_health()
                arms.append(dict(label=label, **data))
        return arms

    def leader_control_status(self):
        return [{"name": u.name, **u.agent.hil_leader_status()}
                for u in self.units if hasattr(u.agent, "hil_leader_status")]

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
        max_state_age=0.25,
    ):
        by_name = {u.name: u for u in units}
        if set(by_name) != {"left", "right"} or len(units) != 2:
            raise ValueError("exactly left and right YAM followers required")
        self.units = [by_name["left"], by_name["right"]]
        self.leaders_connected = all(u.agent is not None for u in self.units)
        if not self.leaders_connected and any(u.agent is not None for u in self.units):
            raise ValueError("connect both Leaders or neither")
        self.mock = mock
        self.max_state_age = max_state_age
        # Every real arm has its own CAN interface and i2rt state lock. Waiting for
        # those four independent snapshots serially adds their lock-contention times
        # together and made a nominal 30 Hz tick take 36-77 ms on RK3588. Reads are
        # side-effect free, so issue them together; writes remain below in one ordered
        # control-owner thread.
        self._read_pool = (
            None
            if mock
            else ThreadPoolExecutor(
                max_workers=4,  # Threads are created lazily when Leaders are attached.
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
        self._manual = self.leaders_connected
        self._idle_leader_hold = None
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

    def set_leaders(self, agents):
        """Tick-boundary ownership transfer; never reconstructs a Follower."""
        if len(agents) != 2 or (agents[0] is None) != (agents[1] is None):
            raise ValueError("attach both Leaders or neither")
        for unit, agent in zip(self.units, agents, strict=True):
            unit.agent = agent
        self.leaders_connected = agents[0] is not None
        self._manual = False
        self._idle_leader_hold = None
        if self.mock and self.leaders_connected:
            self._mock_leaders = np.concatenate([u.robot.get_joint_pos() for u in self.units])

    def read(self):
        self.gripper_feedback = [None, None]
        if self.mock:
            q = vector(np.concatenate([u.robot.get_joint_pos() for u in self.units]))
            if self._manual:
                self._mock_t += 1
                self._mock_leaders[[0, 7]] += 0.001 * np.cos(self._mock_t / 30)
            ages = [0.0, 0.0 if self.leaders_connected else None] * 2
            return q, self._mock_leaders.copy(), [[False, False], [False, False]], ages

        def read_follower(unit):
            started = time.monotonic()
            diagnostic = getattr(unit.robot, "hil_read_with_gripper", None)
            if getattr(self, "grasp_diagnostics_enabled", False) and callable(diagnostic):
                pos, age, feedback = diagnostic()
                return pos, age, time.monotonic() - started, feedback
            read = getattr(unit.robot, "hil_read", None)
            if callable(read):
                pos, age = read()
            else:
                pos = unit.robot.get_joint_pos()
                age = unit.robot.feedback_age()
            return pos, age, time.monotonic() - started, None

        def read_leader(unit):
            started = time.monotonic()
            pos, keys, age = unit.agent.hil_read()
            return pos, keys, age, time.monotonic() - started

        def labelled_read(read, unit, role):
            try:
                return read(unit)
            except Exception as exc:
                side = "左" if unit.name == "left" else "右"
                raise RuntimeError(f"{side} {role}: {exc}") from exc

        started = time.monotonic()
        follower_futures = [self._read_pool.submit(labelled_read, read_follower, unit, "Follower")
                            for unit in self.units]
        leader_futures = [self._read_pool.submit(labelled_read, read_leader, unit, "Leader")
                          if unit.agent is not None else None for unit in self.units]
        followers = [future.result() for future in follower_futures]
        self.gripper_feedback = [result[3] for result in followers]
        leaders_read = [future.result() if future is not None else None for future in leader_futures]
        self.read_timings_s = {
            **{
                f"{unit.name}_follower": result[2]
                for unit, result in zip(self.units, followers, strict=True)
            },
            **{
                f"{unit.name}_leader": result[3]
                for unit, result in zip(self.units, leaders_read, strict=True)
                if result is not None
            },
            "parallel_total": time.monotonic() - started,
        }
        q = vector(np.concatenate([result[0] for result in followers]))
        # Fixed-shape internal arbitration input only. Absent Leaders are recorded
        # as null/__valid=false, never as these placeholder positions.
        leaders = [result[0] if result is not None else np.zeros(7) for result in leaders_read]
        buttons = [[bool(key) for key in result[1][:2]] if result is not None else [False, False]
                   for result in leaders_read]
        ages = [
            age
            for pair in zip(
                [result[1] for result in followers],
                [result[2] if result is not None else None for result in leaders_read],
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
            limiter = getattr(self.units[i].robot, "limit_hil_target", None)
            if callable(limiter):
                target[i * 7:i * 7 + 7] = limiter(target[i * 7:i * 7 + 7])
        return target

    def policy_limits(self):
        """Report actual loaded SDK bounds; do not invent a per-tick slew limit."""
        lower, upper = [], []
        for unit, limits in zip(self.units, self._limits, strict=True):
            margin = getattr(unit.robot, "_gripper_command_margin", 0.0)
            lower.extend([*limits[:, 0], margin])
            upper.extend([*limits[:, 1], 1.0 - margin])
        return dict(lower=lower, upper=upper, joint_unit="radian",
            gripper_motor_limits_rad=[getattr(unit.robot, "gripper_limits", lambda: None)()
                                      for unit in self.units],
            gripper_unit="0_closed_1_open", action_dt_s=1 / 30,
            max_target_step=None, target_slew_limited=False,
            range_implied_max_target_step=(np.asarray(upper) - lower).tolist(),
            command_scope="normalized_SDK_input_not_internal_force_limiter_motor_target")

    def apply(
        self, decision, q, leader, *, dt, mirror=True, maintenance_leader=None, gravity=False,
        leader_homing=False,
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
        submitted = target
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
        manual = decision.leader_manual and maintenance_leader is None and not decision.leader_freeze
        idle_hold = (self.leaders_connected and maintenance_leader is None and not gravity
            and not decision.leader_freeze and (getattr(decision, "phase", None) == "hold"
                or (not mirror and not decision.leader_manual)))
        if idle_hold:
            if self._idle_leader_hold is None:
                self._idle_leader_hold = leader.copy()
            manual = False
        else:
            self._idle_leader_hold = None
        leader_targets = []
        for i, limits in enumerate(self._limits):
            sl = slice(i * 7, i * 7 + 6)
            # HIL policy arms share the final joint target, not delayed feedback.
            # A measured-position-relative clamp caps PD error on every tick and
            # can leave a low-gain leader stationary. Teaching handles remain inputs.
            # Optional high-rate follower control supplies its latest sampled target;
            # this does not make leader writes high-rate or hardware-synchronous.
            arm = submitted[sl] if decision.source == "policy" else np.clip(
                q[sl], leader[sl] - self.leader_speed * dt, leader[sl] + self.leader_speed * dt
            )
            if decision.leader_freeze:
                frozen = getattr(decision, "leader_hold_target", None)
                arm = leader[sl] if frozen is None else frozen[sl]
            if idle_hold:
                arm = self._idle_leader_hold[sl]
            if maintenance_leader is not None:
                # Maintenance owns its bounded trajectory and tracking guard;
                # re-clamping against feedback here would prevent it progressing.
                arm = maintenance_leader[sl]
            leader_targets.append(np.clip(arm, limits[:, 0], limits[:, 1]))
        stamps = {}
        self.write_timings_s = {}
        rules = getattr(self, "interaction_rules", interaction_rules)
        gain = self.leader_gain
        if idle_hold:
            gain = rules.POLICY_GAIN
        if (mirror and (decision.source == "policy" or getattr(decision, "phase", None) == "resume")
                and maintenance_leader is None):
            gain = rules.POLICY_GAIN
        if leader_homing and maintenance_leader is not None:
            # Homing uses native SDK position gains, as follower positioning does.
            # Keep manual gravity compensation and ordinary HOLD independent.
            gain = 1.0
        elif decision.leader_freeze and maintenance_leader is None:
            gain = rules.POLICY_GAIN if (getattr(decision, "phase", None) == "takeover"
                or getattr(decision, "leader_native_hold", False)) else rules.HOLD_GAIN
        for i, u in enumerate(self.units):
            if u.agent is None:
                continue
            started_at = time.monotonic()
            if self.mock:
                if not manual:
                    self._mock_leaders[i * 7 : i * 7 + 6] = leader_targets[i]
            else:
                u.agent.hil_leader_command(
                    leader_targets[i], manual=manual,
                    gain_scale=gain,
                )
            stamps[f"{u.name}_leader"] = time.monotonic()
            self.write_timings_s[f"{u.name}_leader"] = stamps[f"{u.name}_leader"] - started_at
        self._manual = manual and self.leaders_connected
        self.last_leader_target = np.asarray(leader).copy()
        for i in (0, 1):
            self.last_leader_target[i*7:i*7+6] = leader_targets[i]
        self.leader_position_controlled = self.leaders_connected and not manual
        if policy_trajectory:
            for u in self.units:
                stamps[f"{u.name}_follower"] = time.monotonic()
        else:
            submitted, arm_stamps = self._write_followers(target, gravity=gravity, timing=True)
            for name, times in arm_stamps.items():
                stamps[f"{name}_follower"] = times["sdk_call_returned_at"]
                self.write_timings_s[f"{name}_follower"] = times["sdk_call_returned_at"] - times["sdk_call_started_at"]
        return submitted, stamps

    def take_policy_trace(self):
        trace, self._policy_trace = self._policy_trace, None
        return trace

    def _write_followers(self, target, *, gravity=False, timing=False):
        target = self.limit_policy_target(target)
        arm_stamps = {} if timing else None
        for i, u in enumerate(self.units):
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

    def _stop_trajectory(self):
        errors = []
        if self._policy_trajectory is not None:
            errors = close_resources([("policy trajectory", self._policy_trajectory.close)])
            if not self._policy_trajectory.alive:
                self._policy_trajectory = None
        return errors

    def hold(self):
        """Position HOLD on healthy arms, never implicit manual/gravity mode.

        Reject stale hold targets separately, but still attempt the other arms.
        SDK acceptance alone is not proof of physical holding or tracking.
        """
        errors = self._stop_trajectory()
        if self._policy_trajectory is not None:
            return errors  # Never add a second writer while the first still lives.
        for u in self.units:
            try:
                if self.mock:
                    position = u.robot.get_joint_pos()
                else:
                    position, age = u.robot.hil_read()
                    self._check_hold_feedback(position, age)
                u.robot.command_joint_pos(position)
            except Exception as exc:
                errors.append(f"{u.name} follower: {exc}")
            if not self.mock and u.agent is not None:
                try:
                    position, _, age = u.agent.hil_read()
                    self._check_hold_feedback(position, age)
                    u.agent.hil_leader_command(
                        position[:6], manual=False,
                        gain_scale=getattr(self, "interaction_rules", interaction_rules).HOLD_GAIN,
                    )
                except Exception as exc:
                    errors.append(f"{u.name} leader: {exc}")
        self._manual = False
        return errors

    def _check_hold_feedback(self, position, age):
        if not np.isfinite(age) or not 0 <= age <= self.max_state_age:
            raise RuntimeError("反馈过期，不能确认位置保持；请现场支撑机械臂")
        position = np.asarray(position)
        if position.shape != (7,) or not np.isfinite(position).all():
            raise RuntimeError("反馈无效，不能确认位置保持；请现场支撑机械臂")

    def close(self):
        # This is explicit release, not HOLD. Runtime has already requested HOLD;
        # stale feedback must not prevent closing an otherwise releasable SDK.
        errors = self._stop_trajectory()
        if self._policy_trajectory is not None:
            return errors
        lifecycle = getattr(self, "leader_connection", None)
        if lifecycle is not None:
            errors += lifecycle.close()
            if lifecycle.busy:
                return errors
        if self._read_pool is not None:
            errors += close_resources([
                ("state readers", lambda: self._read_pool.shutdown(wait=True, cancel_futures=True))
            ])
            self._read_pool = None
        resources = []
        for u in self.units:
            for role, device in (("leader", u.agent), ("follower", u.robot)):
                if device is None:
                    continue
                close = getattr(device, "close_hil", None)
                if close:
                    resources.append((f"{u.name} {role}", close))
                elif self.mock:
                    resources.append((f"{u.name} {role}", device.stop))
        return errors + close_resources(resources)
