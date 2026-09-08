"""Practical D405 arrival-time pairing and bounded robot state history.

This initial implementation does not estimate exposure clock offsets. All
reported skew is explicitly software arrival skew, not exposure skew.
"""

from collections import deque

import numpy as np


class Observations:
    def __init__(self, cameras, *, max_age=0.5, max_skew=0.12, warn_skew=0.04):
        self.cameras = {c.role: c for c in cameras}
        if set(self.cameras) != {"top", "left", "right"} or len(cameras) != 3:
            raise ValueError("exactly top/left/right cameras required")
        self.max_age, self.max_skew, self.warn_skew = max_age, max_skew, warn_skew
        self.states = deque(maxlen=128)
        self.sequence = 0
        self._last_frames = None

    def add_state(self, now, q):
        self.states.append((now, q.copy()))

    def snapshot(self, now, prompt):
        history = {r: c.history() for r, c in self.cameras.items()}
        if any(not h for h in history.values()) or not self.states:
            return None
        anchor = min(h[-1].meta["host_received_at"] for h in history.values())
        frames = {
            r: min(h, key=lambda f: abs(f.meta["host_received_at"] - anchor))
            for r, h in history.items()
        }
        times = [f.meta["host_received_at"] for f in frames.values()]
        age, skew = now - min(times), max(times) - min(times)
        if max(times) > now or age < 0 or age > self.max_age or skew > self.max_skew:
            return None
        stamps = np.array([t for t, _ in self.states])
        # Only interpolate observed history. Startup waits until it brackets anchor.
        if anchor < stamps[0] or anchor > stamps[-1]:
            return None
        right = int(np.searchsorted(stamps, anchor))
        left = max(0, right - 1)
        t0, q0 = self.states[left]
        t1, q1 = self.states[right]
        alpha = 0 if t1 == t0 else (anchor - t0) / (t1 - t0)
        q = q0 * (1 - alpha) + q1 * alpha
        frame_ids = tuple(f.meta["sequence"] for f in frames.values())
        if frame_ids != self._last_frames:
            self.sequence += 1
            self._last_frames = frame_ids
        images = {r: f.images["rgb"] for r, f in frames.items()}
        obs = {"observation.state": q, "prompt": prompt}
        obs.update({f"observation.images.{r}_rgb": im for r, im in images.items()})
        quality = {
            "age_s": age,
            "arrival_skew_s": skew,
            "sync_warning": skew > self.warn_skew,
            "reference_time": anchor,
            "state_bracket": [t0, t1],
            "cameras": {r: dict(f.meta) for r, f in frames.items()},
        }
        return self.sequence, anchor, obs, images, quality
