"""Exclusive recovery/teaching control, evaluated by the same 30 Hz owner.

Factory home means six zero arm joints, never motor encoder recalibration.
Stations without factory-zero mode may still use a taught ready pose.
"""

import numpy as np

from .core import vector


class Maintenance:
    def __init__(self, *, factory_zero=False):
        self.factory_zero = factory_zero
        self.latched = False
        self.state = "idle"
        self.ready = (
            {"follower": np.zeros(14).tolist(), "leader": np.zeros(14).tolist()}
            if factory_zero
            else None
        )
        self.ready_version = 0
        self.started = 0.0
        self.error = None
        self.grippers = None
        self.frozen = None
        self.home_start = None
        self.home_duration = 0.0

    def capture(self, q, leader):
        if self.factory_zero:
            raise ValueError("此设备固定使用官方关节零位，无需保存准备位")
        self.ready = {"follower": vector(q).tolist(), "leader": vector(leader).tolist()}
        self.ready_version += 1

    def load(self, profile):
        if self.factory_zero:
            return
        self.ready = {key: vector(profile[key]).tolist() for key in ("follower", "leader")}

    def command(self, event, q, leader, *, now, paused):
        if event == "stop":
            self.latched = True
            self.frozen = (q.copy(), leader.copy())
            self.state = "idle"
            self.home_start = None
            return "hold"
        if event == "reset_stop":
            self.latched = False
            self.state = "idle"
            return "hold"
        if event == "hold" or (event and event.startswith("mode:")):
            self.state = "idle"
            self.home_start = None
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
            if event == "home":
                target = vector(self.ready["follower"])
                lead_target = vector(self.ready["leader"])
                joints = [0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12]
                distance = max(
                    np.max(np.abs(target[joints] - q[joints])),
                    np.max(np.abs(lead_target[joints] - leader[joints])),
                )
                self.home_start = (q.copy(), leader.copy())
                self.home_duration = distance / 0.12
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
            self.home_start = None
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
            self.home_start = None
            return None
        start, lead_start = self.home_start
        progress = min(1.0, (now - self.started) / max(self.home_duration, dt))
        planned = start + (target - start) * progress
        lead_planned = lead_start + (lead_target - lead_start) * progress
        # Unlike feedback-relative stepping, a time-based target keeps advancing
        # through motor deadband, matching i2rt's move_joints interpolation. Stop
        # instead of accumulating a large hidden error if any arm cannot follow.
        tracking_error = max(
            np.max(np.abs(planned[joints] - q[joints])),
            np.max(np.abs(lead_planned[joints] - leader[joints])),
        )
        if tracking_error > 0.15:
            self.state = "idle"
            self.home_start = None
            self.error = "回零反馈未跟随规划，已暂停；请检查阻挡或电机状态"
            return None
        return planned, lead_planned
