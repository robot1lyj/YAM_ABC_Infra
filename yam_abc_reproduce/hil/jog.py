"""Bounded follower jogs; applied exclusively by the control-loop owner."""

import queue

import numpy as np


class Jog:
    def __init__(self):
        self.queue = queue.Queue(maxsize=1)
        self.target = None
        self.expires = 0.0

    def request(self, arm, joint, delta):
        if arm not in ("left", "right") or type(joint) is not int or not 0 <= joint <= 6:
            raise ValueError("请选择Follower的关节0–5或夹爪6")
        limit = 0.1 if joint == 6 else np.deg2rad(2)
        if not np.isfinite(delta) or not 0 < abs(delta) <= limit + 1e-9:
            raise ValueError("单步最多2°，夹爪最多10%")
        self.queue.put_nowait((joint + (7 if arm == "right" else 0), float(delta)))

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
            index, delta = self.queue.get_nowait()
            self.target = q.copy()
            self.target[index] += delta
            self.target[[6, 13]] = np.clip(self.target[[6, 13]], 0, 1)
            self.expires = now + 1.0
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
        if np.max(np.abs(result - self.target)) < 1e-6:
            self.target = None
        return result
