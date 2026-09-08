"""Exclusive recovery/teaching control, evaluated by the same 30 Hz owner.

Home means a taught ready pose, never encoder calibration or all-zero joints.
"""

import numpy as np

from .core import vector


class Maintenance:
    def __init__(self):
        self.latched = False
        self.state = "idle"
        self.ready = None
        self.ready_version = 0
        self.started = 0.0
        self.error = None
        self.grippers = None
        self.frozen = None

    def capture(self, q, leader):
        self.ready = {"follower": vector(q).tolist(), "leader": vector(leader).tolist()}
        self.ready_version += 1

    def load(self, profile):
        self.ready = {key: vector(profile[key]).tolist() for key in ("follower", "leader")}

    def command(self, event, q, leader, *, now, paused):
        if event == "stop":
            self.latched = True
            self.frozen = (q.copy(), leader.copy())
            self.state = "idle"
            return "hold"
        if event == "reset_stop":
            self.latched = False
            self.state = "idle"
            return "hold"
        if event == "hold" or (event and event.startswith("mode:")):
            self.state = "idle"
        if self.latched:
            return "hold"
        if event == "capture_home":
            if not paused or self.state != "idle":
                raise ValueError("请先暂停并结束录制，再保存准备位")
            self.capture(q, leader)
            return "hold"
        if event in ("home", "gravity"):
            if not paused:
                raise ValueError("请先暂停并结束录制")
            if event == "home" and self.ready is None:
                raise ValueError("请先示教并保存准备位")
            self.state = "homing" if event == "home" else "gravity"
            self.started = now
            self.error = None
            self.grippers = q[[6, 13]].copy()
            return "hold"
        if self.state != "idle":
            return "hold"
        return event

    def step(self, q, leader, *, now, dt):
        if self.latched:
            return self.frozen
        if self.state != "homing":
            return None
        if now - self.started > 60:
            self.state = "idle"
            self.error = "回准备位超时，已暂停；请检查阻挡和反馈"
            return None
        target, lead_target = vector(self.ready["follower"]), vector(self.ready["leader"])
        # Home arm joints only. Never open a gripper holding a part.
        target[[6, 13]] = self.grippers
        lead_target[[6, 13]] = leader[[6, 13]]
        joints = [0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12]
        distance = max(
            np.max(np.abs(target[joints] - q[joints])),
            np.max(np.abs(lead_target[joints] - leader[joints])),
        )
        if distance <= 0.015:
            self.state = "idle"
            return None
        # One shared interpolation fraction coordinates all four arms.
        alpha = min(1.0, 0.12 * dt / distance)
        return q + (target - q) * alpha, leader + (lead_target - leader) * alpha
