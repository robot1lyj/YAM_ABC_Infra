"""Timestamped non-RTC policy chunks; only the control owner reads or writes this buffer.

Index zero targets the originating observation time. The newest response replaces
the future plan; optional fusion only compares predictions for the same target
time. No previously executed prefix or held last action is replayed.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import exp, floor
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .core import Request


GRIPPERS = (6, 13)


@dataclass(frozen=True)
class TimedChunk:
    token: Request
    origin: float
    first_index: int
    actions: np.ndarray

    def target(self, index: int, action_dt: float) -> float:
        return self.origin + index * action_dt


class ActionBuffer:
    """Small, bounded history of chunks with observation-clock alignment.

    ``raw`` and ``smooth`` execute the newest chunk. ``ensemble`` combines up
    to ``ensemble_chunks`` recent predictions for a matching target time.
    Grippers always use the newest chunk rather than averaging open/close targets.
    """

    def __init__(
        self,
        action_dt: float,
        *,
        fusion: str = "raw",
        smooth_steps: int = 8,
        ensemble_chunks: int = 3,
        ensemble_decay: float = 0.5,
        max_action_age: float = 1.0,
    ):
        if fusion not in ("raw", "smooth", "ensemble"):
            raise ValueError("policy fusion must be raw, smooth or ensemble")
        if not isinstance(smooth_steps, int) or not 1 <= smooth_steps <= 50:
            raise ValueError("smooth_steps must be 1-50")
        if not isinstance(ensemble_chunks, int) or not 2 <= ensemble_chunks <= 5:
            raise ValueError("ensemble_chunks must be 2-5")
        if not np.isfinite(ensemble_decay) or ensemble_decay <= 0:
            raise ValueError("ensemble_decay must be finite and positive")
        if not np.isfinite(max_action_age) or max_action_age <= 0:
            raise ValueError("max_action_age must be finite and positive")
        self.action_dt = action_dt
        self.fusion = fusion
        self.smooth_steps = smooth_steps
        self.ensemble_chunks = ensemble_chunks
        self.ensemble_decay = ensemble_decay
        self.max_action_age = max_action_age
        self.chunks: list[TimedChunk] = []

    def clear(self):
        self.chunks.clear()

    def _index_at(self, chunk: TimedChunk, now: float) -> int:
        # Treat a target exactly on the boundary as due, despite binary rounding.
        return floor((now - chunk.origin) / self.action_dt + 1e-9)

    def _matching(self, chunk: TimedChunk, target: float) -> np.ndarray | None:
        index = round((target - chunk.origin) / self.action_dt)
        if index < chunk.first_index or index >= chunk.first_index + len(chunk.actions):
            return None
        # Never blend adjacent predicted steps merely because they are close.
        if abs(chunk.target(index, self.action_dt) - target) > self.action_dt * 0.25:
            return None
        return chunk.actions[index - chunk.first_index]

    def integrate(self, token: Request, actions: np.ndarray, origin: float, now: float) -> bool:
        first = max(0, floor((now - origin) / self.action_dt + 1e-9))
        if first >= len(actions):
            return False
        # Physically remove the expired prefix while retaining its original
        # model index for provenance and future timestamp matching.
        rows = actions[first:].copy()
        if self.fusion == "smooth" and self.chunks and now - self.chunks[-1].origin <= self.max_action_age:
            old = self.chunks[-1]
            matched = 0
            for index in range(first, min(len(actions), first + self.smooth_steps)):
                prior = self._matching(old, origin + index * self.action_dt)
                if prior is None:
                    continue
                # A short old-to-new ramp at *matching* target times only.
                old_weight = (
                    0.5 if self.smooth_steps == 1 else
                    max(0.0, 1.0 - matched / (self.smooth_steps - 1))
                )
                offset = index - first
                rows[offset] = old_weight * prior + (1.0 - old_weight) * rows[offset]
                rows[offset, list(GRIPPERS)] = actions[index, list(GRIPPERS)]
                matched += 1
        chunk = TimedChunk(token, origin, first, rows)
        if self.fusion == "ensemble":
            self.chunks.append(chunk)
            del self.chunks[:-self.ensemble_chunks]
        else:
            self.chunks = [chunk]
        return True

    def current(self, now: float) -> tuple[np.ndarray, int, Request] | None:
        if not self.chunks:
            return None
        newest = self.chunks[-1]
        index = self._index_at(newest, now)
        if index < newest.first_index or index >= newest.first_index + len(newest.actions):
            return None
        action = newest.actions[index - newest.first_index].copy()
        if self.fusion == "ensemble":
            target = newest.target(index, self.action_dt)
            candidates = [action]
            weights = [1.0]
            for rank, older in enumerate(reversed(self.chunks[:-1]), start=1):
                if now - older.origin > self.max_action_age:
                    continue
                prior = self._matching(older, target)
                if prior is not None:
                    candidates.append(prior)
                    weights.append(exp(-self.ensemble_decay * rank))
            if len(candidates) > 1:
                joints = [i for i in range(14) if i not in GRIPPERS]
                action[joints] = np.average(
                    np.stack([row[joints] for row in candidates]),
                    axis=0,
                    weights=weights,
                )
        return action, index, newest.token

    def remaining(self, now: float) -> int:
        if not self.chunks:
            return 0
        newest = self.chunks[-1]
        return max(
            0,
            newest.first_index + len(newest.actions)
            - max(newest.first_index, self._index_at(newest, now)),
        )

    def seconds_to_expiry(self, now: float) -> float:
        """Usable horizon, limited by both H50 and the existing action-age guard."""
        if not self.chunks:
            return 0.0
        newest = self.chunks[-1]
        index = self._index_at(newest, now)
        if index < newest.first_index or index >= newest.first_index + len(newest.actions):
            return 0.0
        end = min(
            newest.origin + (newest.first_index + len(newest.actions)) * self.action_dt,
            newest.origin + self.max_action_age,
        )
        return max(0.0, end - now)
