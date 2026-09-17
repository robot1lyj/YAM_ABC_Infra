"""30 Hz trained-RTC action ownership, independent of network and motor IO.

The caller supplies the observation's policy tick and records the *submitted*
target after SDK limit clipping. A request freezes every not-yet-executed
target in its prefix; a reply may replace only ticks after that prefix.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class RtcCommitment:
    observation_tick: int
    delay_steps: int
    takeover_tick: int
    actions: np.ndarray


class RtcTimeline:
    def __init__(self, *, delay_steps: int = 9, max_delay_steps: int = 10):
        if not (type(delay_steps) is int and type(max_delay_steps) is int
                and 0 < delay_steps <= max_delay_steps < 50):
            raise ValueError("RTC delay must fit the trained prefix range")
        self.delay_steps = delay_steps
        self.max_delay_steps = max_delay_steps
        self._submitted = deque(maxlen=64)
        self._committed: dict[int, tuple[np.ndarray, str]] = {}
        self._plan: dict[int, np.ndarray] = {}
        self._last_target: np.ndarray | None = None
        self.pending: RtcCommitment | None = None
        self.has_accepted_plan = False
        self.accepted_replies = 0
        self.late_replies = 0

    def clear(self):
        self._submitted.clear()
        self._committed.clear()
        self._plan.clear()
        self._last_target = None
        self.pending = None
        self.has_accepted_plan = False

    @staticmethod
    def _action(value) -> np.ndarray:
        out = np.asarray(value, dtype=np.float64)
        if out.shape != (14,) or not np.isfinite(out).all():
            raise ValueError("RTC target must be finite 14D")
        out = out.copy()
        out[[6, 13]] = np.clip(out[[6, 13]], 0, 1)
        return out

    def select(self, tick: int, hold_target) -> tuple[np.ndarray, str]:
        """Return the one immutable target for this controller tick."""
        if type(tick) is not int or tick < 0:
            raise ValueError("invalid controller tick")
        if tick in self._committed:
            target, source = self._committed[tick]
            return target.copy(), source
        if tick in self._plan:
            return self._plan[tick].copy(), "policy"
        return self._action(self._last_target if self._last_target is not None else hold_target), "hold"

    def record_submitted(self, tick: int, target):
        """Record the clipped command actually passed to the SDK this tick."""
        action = self._action(target)
        committed = self._committed.pop(tick, None)
        if committed is not None and not np.allclose(
            action, committed[0], rtol=0, atol=2e-6
        ):
            self.clear()
            raise RuntimeError("RTC committed target changed at actuation")
        self._submitted.append((tick, action.copy()))
        self._last_target = action
        self._plan.pop(tick, None)
        for old in tuple(self._plan):
            if old < tick:
                del self._plan[old]
        for old in tuple(self._committed):
            if old < tick:
                del self._committed[old]

    def prepare(self, *, observation_tick: int, current_tick: int, limit_target) -> RtcCommitment:
        """Freeze N..N+d-1 from actual history and limited future targets.

        A historical gap is a hard error, not permission to invent a prefix.
        `limit_target` must use the same joint limits as the SDK write path.
        """
        if self.pending is not None:
            raise RuntimeError("RTC permits only one in-flight request")
        if (type(observation_tick) is not int or type(current_tick) is not int
                or observation_tick < 0 or observation_tick > current_tick):
            raise ValueError("invalid observation/controller tick mapping")
        takeover = observation_tick + self.delay_steps
        if takeover <= current_tick:
            raise ValueError("RTC observation is too old for the configured delay")
        history = dict(self._submitted)
        future = {}
        prefix = []
        for tick in range(observation_tick, takeover):
            if tick <= current_tick:
                if tick not in history:
                    raise ValueError("RTC submitted action history has a gap")
                target = history[tick]
            else:
                selected, source = self.select(tick, self._last_target)
                target = self._action(limit_target(selected))
                future[tick] = (target, source)
            prefix.append(target.copy())
        self._committed.update(future)
        commitment = RtcCommitment(
            observation_tick, self.delay_steps, takeover,
            np.asarray(prefix, dtype=np.float32),
        )
        self.pending = commitment
        return commitment

    def install(self, commitment: RtcCommitment, actions, *, current_tick: int,
                limit_target) -> bool:
        """Reject a stale reply; never slide its fixed takeover tick forward."""
        if commitment is not self.pending:
            return False
        self.pending = None
        if current_tick > commitment.takeover_tick:
            self.late_replies += 1
            return False
        rows = np.asarray(actions, dtype=np.float64)
        if rows.shape != (50, 14) or not np.isfinite(rows).all():
            raise ValueError("RTC reply must be finite (50,14)")
        d = commitment.delay_steps
        if not np.allclose(rows[:d], commitment.actions, rtol=0, atol=2e-6):
            raise ValueError("RTC reply modified committed prefix")
        self._plan = {
            # Thor may return finite gripper values outside [0,1]. Apply the
            # controller's semantic gripper clamp before StationIO's strict
            # target validator, then apply the same SDK joint limits as writes.
            commitment.observation_tick + index: self._action(
                limit_target(self._action(row))
            )
            for index, row in enumerate(rows[d:], start=d)
        }
        self.has_accepted_plan = True
        self.accepted_replies += 1
        return True

    def remaining(self, tick: int) -> int:
        return sum(index >= tick for index in self._plan)

    def has_target(self, tick: int) -> bool:
        return tick in self._committed or tick in self._plan
