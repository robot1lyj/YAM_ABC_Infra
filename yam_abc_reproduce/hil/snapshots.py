"""Bounded software frame pairing using one host monotonic clock.

Receipt timestamps measure software arrival skew, not exposure synchronization.
Arrays must remain immutable while present in these buffers or an RPC snapshot.
"""

from collections import deque
from dataclasses import dataclass
from threading import Lock

import numpy as np


@dataclass(frozen=True)
class Frame:
    sequence: int
    received_at: float
    image: np.ndarray


class FrameBuffers:
    def __init__(self, roles=("top", "left", "right"), capacity=4):
        if capacity < 1 or len(set(roles)) != len(roles) or not roles:
            raise ValueError("nonempty unique roles and positive capacity required")
        self._buffers = {role: deque(maxlen=capacity) for role in roles}
        self._lock = Lock()

    def publish(self, role, frame: Frame):
        if not np.isfinite(frame.received_at):
            raise ValueError("finite host timestamp required")
        with self._lock:
            buf = self._buffers[role]
            if buf and (
                frame.sequence <= buf[-1].sequence or frame.received_at < buf[-1].received_at
            ):
                raise ValueError("nonmonotonic frame")
            buf.append(frame)

    def snapshot(self, now, *, max_age=0.15, max_skew=0.04):
        if not all(np.isfinite(v) for v in (now, max_age, max_skew)) or min(max_age, max_skew) < 0:
            raise ValueError("invalid time limits")
        with self._lock:
            if any(not b for b in self._buffers.values()):
                return None
            anchor = min(b[-1].received_at for b in self._buffers.values())
            chosen = {
                r: min(b, key=lambda f: abs(f.received_at - anchor))
                for r, b in self._buffers.items()
            }
        stamps = [f.received_at for f in chosen.values()]
        if (
            any(t > now or now - t > max_age for t in stamps)
            or max(stamps) - min(stamps) > max_skew
        ):
            return None
        return chosen
