"""Four-arm IO adapter, with all motor writes owned by the runtime tick."""

import time

import numpy as np

from .core import vector


class StationIO:
    def __init__(self, units, *, mock=False, leader_gain=0.2, leader_speed=0.5):
        by_name = {u.name: u for u in units}
        if set(by_name) != {"left", "right"} or len(units) != 2:
            raise ValueError("exactly left and right YAM followers required")
        self.units = [by_name["left"], by_name["right"]]
        self.mock = mock
        self.leader_gain, self.leader_speed = leader_gain, leader_speed
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
        q = vector(np.concatenate([u.robot.get_joint_pos() for u in self.units]))
        if self.mock:
            if self._manual:
                self._mock_t += 1
                self._mock_leaders[[0, 7]] += 0.001 * np.cos(self._mock_t / 30)
            return q, self._mock_leaders.copy(), [False, False], [0.0] * 4
        leaders, buttons, ages = [], [False, False], []
        for u in self.units:
            leader, keys, age = u.agent.hil_read()
            leaders.append(leader)
            buttons = [a or b for a, b in zip(buttons, keys[:2])]
            ages.extend([u.robot.feedback_age(), age])
        return q, vector(np.concatenate(leaders)), buttons, ages

    def apply(self, decision, q, leader, *, dt, mirror=True):
        target = vector(decision.action)
        # Validate/clamp both arms before sending any part of this tick.
        for i, limits in enumerate(self._limits):
            sl = slice(i * 7, i * 7 + 6)
            target[sl] = np.clip(target[sl], limits[:, 0], limits[:, 1])
        manual = decision.leader_manual or not mirror
        leader_targets = []
        for i, limits in enumerate(self._limits):
            sl = slice(i * 7, i * 7 + 6)
            # Mirror actual follower pose, including limited catch-up on RESUME.
            arm = np.clip(
                q[sl], leader[sl] - self.leader_speed * dt, leader[sl] + self.leader_speed * dt
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
        for i, u in enumerate(self.units):
            u.robot.command_joint_pos(target[i * 7 : i * 7 + 7])
            stamps[f"{u.name}_follower"] = time.monotonic()
        return target, stamps

    def hold(self):
        errors = []
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
