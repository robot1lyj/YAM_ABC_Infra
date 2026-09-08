"""Deterministic, hardware-free action arbitration for the first HIL version.

No CAN, network, video IO or inference runs here. The future real-time owner calls
step once per tick and executes its decision. All angles must already be in the
agreed hardware units. This module does not establish real-device safety.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np


class Mode(StrEnum):
    TELEOP = "teleop"
    INFERENCE = "inference"
    HIL = "hil"


class Phase(StrEnum):
    HOLD = "hold"
    POLICY = "policy"
    HUMAN = "human"
    RESUME = "resume"
    FAULT = "fault"


@dataclass(frozen=True)
class Request:
    epoch: int
    request_id: int
    observation_id: int
    created_at: float  # controller monotonic seconds, never Thor wall time


@dataclass
class Decision:
    action: np.ndarray
    selected_action: np.ndarray
    policy_action: np.ndarray | None
    phase: Phase
    source: str
    epoch: int
    intervention: bool
    policy_valid: bool
    leader_manual: bool
    gripper_owned: tuple[bool, bool]


def vector(value) -> np.ndarray:
    out = np.asarray(value, dtype=np.float64)
    if out.shape != (14,) or not np.isfinite(out).all():
        raise ValueError("expected finite 14D YAM vector")
    if np.any((out[[6, 13]] < 0) | (out[[6, 13]] > 1)):
        raise ValueError("grippers must be normalized to [0,1]")
    return out.copy()


class Arbiter:
    """Whole-station handover, ordinary chunk execution, no RTC/prefetch.

    Startup is HOLD. start() and toggle() are explicit local events. Every
    transition invalidates pending requests/chunks. A network worker must use
    Request tokens and must never call motor APIs. Deadlines include network
    latency; responses do not reset the originating observation's age.
    """

    def __init__(
        self,
        mode: Mode,
        *,
        execute_steps: int = 10,
        max_joint_speed: float = 0.6,
        max_gripper_speed: float = 1.0,
        max_request_age: float = 0.5,
        max_action_age: float = 1.0,
        handover_error: float = 0.15,
        pickup_tolerance: float = 0.05,
    ):
        values = (
            max_joint_speed,
            max_gripper_speed,
            max_request_age,
            max_action_age,
            handover_error,
            pickup_tolerance,
        )
        if (
            not isinstance(execute_steps, int)
            or not 1 <= execute_steps <= 50
            or not all(np.isfinite(v) and v > 0 for v in values)
        ):
            raise ValueError("invalid limits")
        self.mode = Mode(mode)
        self.phase = Phase.HOLD
        self.execute_steps = execute_steps
        self.max_joint_speed = max_joint_speed
        self.max_gripper_speed = max_gripper_speed
        self.max_request_age = max_request_age
        self.max_action_age = max_action_age
        self.handover_error = handover_error
        self.pickup_tolerance = pickup_tolerance
        self.epoch = 0
        self._serial = 0
        self.pending: Request | None = None
        self._chunk: np.ndarray | None = None
        self._index = 0
        self._origin_time = 0.0
        self._hold: np.ndarray | None = None
        self._pickup = [False, False]
        self._previous_grip: np.ndarray | None = None
        self.fault_reason: str | None = None

    def _transition(self, phase: Phase, state: np.ndarray):
        self.epoch += 1
        self.pending = None
        self._chunk = None
        self._index = 0
        self._hold = vector(state)
        self.phase = phase

    def start(self, state, leader=None):
        if self.phase == Phase.FAULT:
            raise RuntimeError("fault requires explicit session rebuild")
        self._transition(Phase.RESUME, state)
        if self.mode == Mode.TELEOP:
            self._human(state, leader)

    def _human(self, state, leader):
        q, h = vector(state), vector(leader)
        joint = np.ones(14, dtype=bool)
        joint[[6, 13]] = False
        if np.max(np.abs(q[joint] - h[joint])) > self.handover_error:
            self._transition(Phase.HOLD, q)
            return
        self._transition(Phase.HUMAN, q)
        self._pickup = [False, False]
        self._previous_grip = h[[6, 13]].copy()

    def toggle(self, state, leader):
        if self.mode != Mode.HIL or self.phase == Phase.FAULT:
            return
        if self.phase == Phase.HUMAN:
            self._transition(Phase.RESUME, state)
        else:
            self._human(state, leader)

    def fail(self, state, reason: str):
        self.fault_reason = reason
        self._transition(Phase.FAULT, state)

    def request(self, observation_id: int, now: float) -> Request | None:
        if self.phase not in (Phase.POLICY, Phase.RESUME) or self.pending is not None:
            return None
        if self._chunk is not None and self._index < len(self._chunk):
            return None
        if not np.isfinite(now):
            raise ValueError("finite monotonic time required")
        self._serial += 1
        self.pending = Request(self.epoch, self._serial, observation_id, now)
        return self.pending

    def accept(self, token: Request, actions, now: float) -> bool:
        if token != self.pending or token.epoch != self.epoch:
            return False
        age = now - token.created_at
        if not np.isfinite(age) or age < 0 or age > self.max_request_age:
            self._transition(Phase.HOLD, self._hold)
            return False
        rows = np.asarray(actions, dtype=np.float64)
        if rows.shape != (50, 14):
            raise ValueError("policy response must be (50,14)")
        for row in rows:
            vector(row)
        self._chunk = rows[: self.execute_steps].copy()
        self._origin_time = token.created_at
        self._index = 0
        self.pending = None
        return True

    def step(
        self,
        state,
        leader,
        *,
        now: float,
        dt: float,
        observation_fresh: bool = True,
        leader_ready: bool = False,
    ) -> Decision:
        q = vector(state)
        if not np.isfinite(now) or not np.isfinite(dt) or dt <= 0:
            raise ValueError("dt must be finite and positive")
        if self._hold is None:
            self._hold = q.copy()
        if dt > 0.1:
            self.fail(q, "control tick exceeded 100 ms")
        if self.pending and now - self.pending.created_at > self.max_request_age:
            self._transition(Phase.HOLD, q)
        if not observation_fresh and self.phase in (Phase.RESUME, Phase.POLICY):
            self._transition(Phase.HOLD, q)
        policy = None
        source = "hold"
        selected = self._hold.copy()
        if self.phase == Phase.HUMAN:
            selected = vector(leader)
            source = "human"
            for j, dim in enumerate((6, 13)):
                old = self._previous_grip[j]
                new = selected[dim]
                target = self._hold[dim]
                if (
                    abs(new - target) <= self.pickup_tolerance
                    or (old - target) * (new - target) < 0
                ):
                    self._pickup[j] = True
                if not self._pickup[j]:
                    selected[dim] = target
            self._previous_grip = vector(leader)[[6, 13]]
        elif self.phase in (Phase.RESUME, Phase.POLICY) and self._chunk is not None:
            if now - self._origin_time > self.max_action_age:
                self._transition(Phase.HOLD, q)
                selected = q.copy()
            elif self._index < len(self._chunk) and (self.phase == Phase.POLICY or leader_ready):
                policy = self._chunk[self._index].copy()
                selected = policy.copy()
                self._index += 1
                source = "policy"
                self.phase = Phase.POLICY
        # Bound commanded tracking error per nominal tick; this is not a measured velocity guarantee.
        limit = np.full(14, self.max_joint_speed * dt)
        limit[[6, 13]] = self.max_gripper_speed * dt
        action = np.clip(selected, q - limit, q + limit)
        self._hold = action.copy()
        return Decision(
            action,
            selected,
            policy,
            self.phase,
            source,
            self.epoch,
            self.phase == Phase.HUMAN,
            policy is not None,
            self.phase in (Phase.HUMAN, Phase.HOLD, Phase.FAULT),
            tuple(self._pickup),
        )
