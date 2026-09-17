"""Raw timestamped non-RTC chunks; only the control owner reads or writes here."""

from __future__ import annotations

from dataclasses import dataclass
from math import floor
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .core import Request


GRIPPERS = (6, 13)
JOINTS = tuple(index for index in range(14) if index not in GRIPPERS)


@dataclass(frozen=True)
class TimedChunk:
    token: Request
    origin: float
    first_index: int
    actions: np.ndarray

    def target(self, index: int, action_dt: float) -> float:
        return self.origin + index * action_dt


class ActionBuffer:
    """The newest valid chunk replaces the prior plan without fusion."""

    def __init__(
        self,
        action_dt: float,
        *,
        max_action_age: float = 1.0,
    ):
        if not np.isfinite(max_action_age) or max_action_age <= 0:
            raise ValueError("max_action_age must be finite and positive")
        self.action_dt = action_dt
        self.fusion = "raw"
        self.max_action_age = max_action_age
        self.chunk: TimedChunk | None = None
        self.last_trimmed_steps: int | None = None
        self.last_seam_max_rad: float | None = None
        self.last_selection: dict | None = None

    def clear(self):
        self.chunk = None
        self.last_trimmed_steps = None
        self.last_seam_max_rad = None
        self.last_selection = None

    def _index_at(self, chunk: TimedChunk, now: float) -> int:
        # Treat a target exactly on the boundary as due, despite binary rounding.
        return floor((now - chunk.origin) / self.action_dt + 1e-9)

    def _at_target(self, chunk: TimedChunk, target: float) -> np.ndarray | None:
        """Evaluate a chunk at one physical target time.

        Camera observations are not phase-locked to the 30 Hz control clock, so
        two valid prediction grids can be shifted by a fraction of ``action_dt``.
        Linear interpolation evaluates the older joint plan at the *exact* new
        target time instead of either rejecting the overlap or blending adjacent
        timestamps as though they were equal.  Never extrapolate beyond a chunk.
        """
        position = (target - chunk.origin) / self.action_dt
        lower = floor(position + 1e-9)
        fraction = position - lower
        end = chunk.first_index + len(chunk.actions)
        if lower < chunk.first_index or lower >= end:
            return None
        first = chunk.actions[lower - chunk.first_index]
        if fraction <= 1e-9:
            return first
        upper = lower + 1
        if upper >= end:
            return None
        second = chunk.actions[upper - chunk.first_index]
        return first * (1.0 - fraction) + second * fraction

    def integrate(self, token: Request, actions: np.ndarray, origin: float, now: float) -> bool:
        first = max(0, floor((now - origin) / self.action_dt + 1e-9))
        if first >= len(actions):
            return False
        # Physically remove the expired prefix while retaining its original
        # model index for provenance and future timestamp matching.
        rows = actions[first:].copy()
        self.last_trimmed_steps = first
        self.last_seam_max_rad = None
        if self.chunk is not None and now - self.chunk.origin <= self.max_action_age:
            old = self.chunk
            seam_prior = self._at_target(old, origin + first * self.action_dt)
            if seam_prior is not None:
                self.last_seam_max_rad = float(
                    np.max(np.abs(actions[first, list(JOINTS)] - seam_prior[list(JOINTS)]))
                )
        self.chunk = TimedChunk(token, origin, first, rows)
        return True

    def current(self, now: float) -> tuple[np.ndarray, int, Request] | None:
        self.last_selection = None
        if self.chunk is None:
            return None
        newest = self.chunk
        index = self._index_at(newest, now)
        if index < newest.first_index or index >= newest.first_index + len(newest.actions):
            return None
        action = newest.actions[index - newest.first_index].copy()
        target_at = newest.target(index, self.action_dt)
        self.last_selection = {
            "fusion": self.fusion,
            "target_at": target_at,
            "joint_sources": [
                {"epoch": newest.token.epoch, "request_id": newest.token.request_id,
                 "observed_at": newest.origin,
                 "model_index": (target_at - newest.origin) / self.action_dt,
                 "weight": 1.0}
            ],
            "gripper_source": {
                "epoch": newest.token.epoch,
                "request_id": newest.token.request_id,
                "observed_at": newest.origin,
                "model_index": (target_at - newest.origin) / self.action_dt,
            },
        }
        return action, index, newest.token

    def remaining(self, now: float) -> int:
        if self.chunk is None:
            return 0
        newest = self.chunk
        return max(
            0,
            newest.first_index + len(newest.actions)
            - max(newest.first_index, self._index_at(newest, now)),
        )

    def seconds_to_expiry(self, now: float) -> float:
        """Usable horizon, limited by both H50 and the existing action-age guard."""
        if self.chunk is None:
            return 0.0
        newest = self.chunk
        index = self._index_at(newest, now)
        if index < newest.first_index or index >= newest.first_index + len(newest.actions):
            return 0.0
        end = min(
            newest.origin + (newest.first_index + len(newest.actions)) * self.action_dt,
            newest.origin + self.max_action_age,
        )
        return max(0.0, end - now)
