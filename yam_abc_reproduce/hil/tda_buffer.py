"""OpenArm-style TDA queue for ordinary (non-RTC) action chunks.

Port of the drop/overlap blend in openarm-vr's ActionChunkQueue. The trained
RTC committed prefix must not pass through this queue.
"""

from __future__ import annotations

import numpy as np


class TdaActionBuffer:
    def __init__(
        self,
        action_dt: float,
        *,
        drop_max: int = 25,
        min_overlap: int = 1,
        blend_mode: str = "linear",
        blend_alpha: float = 0.5,
    ):
        if not np.isfinite(action_dt) or action_dt <= 0:
            raise ValueError("action_dt must be positive")
        if type(drop_max) is not int or drop_max < 0:
            raise ValueError("tda_drop_max must be nonnegative")
        if type(min_overlap) is not int or min_overlap < 0:
            raise ValueError("tda_min_overlap must be nonnegative")
        if blend_mode not in ("linear", "ema"):
            raise ValueError("tda_blend_mode must be linear or ema")
        if not np.isfinite(blend_alpha):
            raise ValueError("tda_blend_alpha must be finite")
        self.action_dt = action_dt
        self.drop_max = drop_max
        self.min_overlap = min_overlap
        self.blend_mode = blend_mode
        self.blend_alpha = float(np.clip(blend_alpha, 0.0, 1.0))
        self.fusion = "tda_smooth"
        self._queue: list[np.ndarray] = []
        self._meta: list[tuple[object, int]] = []
        self.last_trimmed_steps: int | None = None
        self.last_seam_max_rad: float | None = None
        self.last_selection: dict | None = None

    @property
    def chunk(self):
        # Arbiter only tests whether an action plan remains available.
        return self if self._queue else None

    def clear(self):
        self._queue.clear()
        self._meta.clear()
        self.last_trimmed_steps = None
        self.last_seam_max_rad = None
        self.last_selection = None

    def remaining(self, now=None) -> int:
        return len(self._queue)

    def seconds_to_expiry(self, now=None) -> float:
        return len(self._queue) * self.action_dt

    def integrate(self, token, actions: np.ndarray, origin: float, now: float) -> bool:
        """Drop elapsed request steps, then blend old queue tail/new head.

        `origin`/`now` remain in Arbiter's age checks; queue merge intentionally
        uses the exact OpenArm request-queue-consumption rule instead of them.
        """
        chunk = np.asarray(actions, dtype=np.float32)
        if chunk.shape != (50, 14):
            raise ValueError("TDA actions must be (50,14)")
        request_qsize = (
            len(self._queue)
            if token.queue_size_at_request is None
            else max(0, int(token.queue_size_at_request))
        )
        executed_since_request = max(0, request_qsize - len(self._queue))
        drop_count = min(executed_since_request, self.drop_max, len(chunk))
        chunk_meta = [(token, index) for index in range(len(chunk))]
        chunk = chunk[drop_count:]
        chunk_meta = chunk_meta[drop_count:]
        self.last_trimmed_steps = drop_count
        self.last_seam_max_rad = None
        if len(chunk) == 0:
            return bool(self._queue)

        overlap = min(len(self._queue), len(chunk))
        if overlap <= 0 or overlap < self.min_overlap:
            if self._queue:
                self.last_seam_max_rad = float(np.max(np.abs(
                    chunk[0, [0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12]]
                    - self._queue[-1][[0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12]]
                )))
            self._queue.extend(row.copy() for row in chunk)
            self._meta.extend(chunk_meta)
            return True

        old_tail = np.stack(self._queue[-overlap:]).astype(np.float32)
        new_head = chunk[:overlap]
        self.last_seam_max_rad = float(np.max(np.abs(
            old_tail[:, [0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12]]
            - new_head[:, [0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12]]
        )))
        if self.blend_mode == "ema":
            blended = old_tail * (1.0 - self.blend_alpha) + new_head * self.blend_alpha
        else:
            weights = (
                np.array([self.blend_alpha], dtype=np.float32)
                if overlap == 1 else
                np.linspace(1.0 / (overlap + 1.0), overlap / (overlap + 1.0), overlap,
                            dtype=np.float32)
            )
            blended = old_tail * (1.0 - weights[:, None]) + new_head * weights[:, None]
        self._queue[-overlap:] = [row.copy() for row in blended]
        self._meta[-overlap:] = chunk_meta[:overlap]
        if overlap < len(chunk):
            self._queue.extend(row.copy() for row in chunk[overlap:])
            self._meta.extend(chunk_meta[overlap:])
        return True

    def current(self, now: float):
        self.last_selection = None
        if not self._queue:
            return None
        action = self._queue.pop(0).astype(np.float64)
        token, index = self._meta.pop(0)
        self.last_selection = {
            "fusion": "tda_smooth", "request_id": token.request_id,
            "model_index": index,
        }
        return action, index, token
