"""Bounded follower jogs; applied exclusively by the control-loop owner."""

import queue

import numpy as np


class Jog:
    def __init__(self):
        self.queue = queue.Queue(maxsize=1)
        self.target = None
        self.expires = 0.0
        self.absolute = False

    def request(self, arm, joint, delta=None, *, target=None):
        if arm not in ("left", "right") or type(joint) is not int or not 0 <= joint <= 6:
            raise ValueError("请选择Follower的关节0–5或夹爪6")
        if (delta is None) == (target is None):
            raise ValueError("请选择相对点动或夹爪绝对开度，不可同时设置")
        if target is not None:
            if joint != 6 or not np.isfinite(target) or not 0 <= target <= 1:
                raise ValueError("夹爪目标开度须为0至1；关节仍使用小步调试")
            value = target
        else:
            limit = 0.1 if joint == 6 else np.deg2rad(2)
            if not np.isfinite(delta) or not 0 < abs(delta) <= limit + 1e-9:
                raise ValueError("单步最多2°，夹爪最多10%")
            value = delta
        self.queue.put_nowait((joint + (7 if arm == "right" else 0), float(value), target is not None))

    def clear(self):
        self.target = None
        while True:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break

    def step(self, q, *, allowed, now, dt):
        if not allowed:
            self.clear()
            return None
        try:
            index, value, self.absolute = self.queue.get_nowait()
            self.target = q.copy()
            self.target[index] = value if self.absolute else self.target[index] + value
            self.target[[6, 13]] = np.clip(self.target[[6, 13]], 0, 1)
            # Full gripper travel takes 4 s at the existing 0.25/s jog rate.
            # Stop pursuing a blocked target after a bounded maintenance interval.
            self.expires = now + (6.0 if self.absolute else 1.0)
        except queue.Empty:
            pass
        if self.target is None:
            return None
        if now >= self.expires:
            self.target = None
            return None
        speed = np.full(14, 0.1)  # rad/s, conservative discrete adjustment
        speed[[6, 13]] = 0.25  # normalized opening/s
        result = np.clip(self.target, q - speed * dt, q + speed * dt)
        reached = np.max(np.abs(q - self.target)) < 0.001 if self.absolute else np.max(np.abs(result - self.target)) < 1e-6
        if reached:
            self.target = None
        return result
