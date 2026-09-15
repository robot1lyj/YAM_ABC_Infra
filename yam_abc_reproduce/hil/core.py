"""Deterministic, hardware-free action arbitration for the first HIL version.

No CAN, network, video IO or inference runs here. The runtime owner calls
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
    COLLECT = "collect"


class Phase(StrEnum):
    HOLD = "hold"
    POLICY = "policy"
    HUMAN = "human"
    TAKEOVER = "takeover"
    RESUME = "resume"
    FAULT = "fault"


@dataclass(frozen=True)
class Request:
    epoch: int
    request_id: int
    observation_id: int
    created_at: float  # controller monotonic seconds, never Thor wall time
    observed_at: float | None = None


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
    request: Request | None = None
    action_index: int | None = None
    leader_freeze: bool = False


def vector(value) -> np.ndarray:
    out = np.asarray(value, dtype=np.float64)
    if out.shape != (14,) or not np.isfinite(out).all():
        raise ValueError("expected finite 14D YAM vector")
    if np.any((out[[6, 13]] < 0) | (out[[6, 13]] > 1)):
        raise ValueError("grippers must be normalized to [0,1]")
    return out.copy()


class Arbiter:
    """Whole-station handover with baseline or timestamped asynchronous chunks; no RTC.

    Startup is HOLD. start(), takeover() and resume_policy() are explicit local events. Every
    transition invalidates pending requests/chunks. A network worker must use
    Request tokens and must never call motor APIs. Deadlines include network
    latency; responses do not reset the originating observation's age.
    """

    def __init__(
        self,
        mode: Mode,
        *,
        execute_steps: int = 10,
        max_joint_speed: float = 5.0,
        max_manual_joint_speed: float | None = None,
        max_gripper_speed: float = 1.0,
        max_request_age: float = 0.5,
        max_action_age: float = 1.0,
        handover_error: float = 0.15,
        pickup_tolerance: float = 0.05,
        streaming: bool = False,
        action_dt: float = 1 / 30,
        replan_period: float = 0.2,
        tick_timeout: float = 0.1,
    ):
        values = (
            max_joint_speed,
            max_gripper_speed,
            max_request_age,
            max_action_age,
            handover_error,
            pickup_tolerance,
            action_dt,
            replan_period,
            tick_timeout,
        )
        if (
            not isinstance(execute_steps, int)
            or not 1 <= execute_steps <= 50
            or not all(np.isfinite(v) and v > 0 for v in values)
            or (
                max_manual_joint_speed is not None
                and (not np.isfinite(max_manual_joint_speed) or max_manual_joint_speed <= 0)
            )
        ):
            raise ValueError("invalid limits")
        self.streaming = streaming
        self.action_dt = action_dt
        self.replan_period = replan_period
        self.tick_timeout = tick_timeout
        self._last_request_at = -float("inf")
        self._active_request = None
        self.mode = Mode(mode)
        self.phase = Phase.HOLD
        self.execute_steps = execute_steps
        self.max_joint_speed = max_joint_speed
        self.max_manual_joint_speed = max_manual_joint_speed
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
        self._offset = np.zeros(14)
        self._freeze_tick = False

    def _transition(self, phase: Phase, state: np.ndarray):
        self._active_request = None
        self._last_request_at = -float("inf")
        self.epoch += 1
        self.pending = None
        self._chunk = None
        self._index = 0
        self._hold = vector(state)
        self.phase = phase

    def start(self, state, leader=None):
        if self.phase == Phase.FAULT:
            raise RuntimeError("fault requires explicit session rebuild")
        if self.phase != Phase.HOLD:
            return
        self._transition(Phase.RESUME, state)
        if self.mode in (Mode.TELEOP, Mode.COLLECT):
            self._human(state, leader)

    def _human(self, state, leader):
        q, h = vector(state), vector(leader)
        self._transition(Phase.HUMAN, q)
        # Start manual control as a clutch: the follower must not jump to an
        # unrelated absolute leader pose when an operator begins a new episode.
        # Subsequent leader *motion* is mirrored one-for-one from this baseline.
        # Grippers keep their existing soft-pickup behavior below instead of
        # inheriting an angular offset from the teaching-handle trigger.
        self._offset = q - h
        self._offset[[6, 13]] = 0
        self._pickup = [False, False]
        self._previous_grip = h[[6, 13]].copy()

    def takeover(self, state, leader):
        if self.mode != Mode.HIL or self.phase not in (Phase.POLICY, Phase.RESUME):
            return
        q, h = vector(state), vector(leader)
        self._transition(Phase.TAKEOVER, q)
        self._offset = q - h
        self._offset[[6, 13]] = 0
        self._pickup = [False, False]
        self._previous_grip = h[[6, 13]].copy()
        self._freeze_tick = True

    def resume_policy(self, state):
        if self.mode == Mode.HIL and self.phase == Phase.HUMAN:
            self._transition(Phase.RESUME, state)

    def hold(self, state):
        if self.phase != Phase.FAULT:
            self._transition(Phase.HOLD, state)

    def change_mode(self, mode, state):
        if self.phase == Phase.FAULT:
            raise RuntimeError("rebuild session after fault")
        self.mode = Mode(mode)
        self._transition(Phase.HOLD, state)

    def fail(self, state, reason: str):
        self.fault_reason = reason
        self._transition(Phase.FAULT, state)

    def request(
        self, observation_id: int, now: float, observed_at: float | None = None
    ) -> Request | None:
        if self.phase not in (Phase.POLICY, Phase.RESUME) or self.pending is not None:
            return None
        if self.streaming:
            if now - self._last_request_at < self.replan_period:
                return None
        elif self._chunk is not None and self._index < len(self._chunk):
            return None
        if not np.isfinite(now):
            raise ValueError("finite monotonic time required")
        self._serial += 1
        if observed_at is not None and (not np.isfinite(observed_at) or observed_at > now):
            raise ValueError("invalid observation time")
        self.pending = Request(self.epoch, self._serial, observation_id, now, observed_at)
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
        origin = token.observed_at if token.observed_at is not None else token.created_at
        if self.streaming and now - origin >= min(self.max_action_age, len(rows) * self.action_dt):
            self._transition(Phase.HOLD, self._hold)
            return False
        self._active_request = token
        self._chunk = rows.copy() if self.streaming else rows[: self.execute_steps].copy()
        self._origin_time = origin
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
        if dt > self.tick_timeout:
            self.fail(q, "control tick exceeded configured timeout")
        if self.pending and now - self.pending.created_at > self.max_request_age:
            self._transition(Phase.HOLD, q)
        if not observation_fresh and self.phase in (Phase.RESUME, Phase.POLICY):
            self._transition(Phase.HOLD, q)
        if self.phase == Phase.TAKEOVER:
            if self._freeze_tick:
                self._freeze_tick = False
            else:
                self.phase = Phase.HUMAN
        policy = None
        action_index = None
        source = "hold"
        selected = self._hold.copy()
        if self.phase == Phase.HUMAN:
            selected = vector(leader) + self._offset
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
            if self.streaming:
                self._index = max(0, int((now - self._origin_time) / self.action_dt))
            if self.streaming and self._index >= len(self._chunk):
                self._transition(Phase.HOLD, q)
                selected = q.copy()
            elif now - self._origin_time > self.max_action_age:
                self._transition(Phase.HOLD, q)
                selected = q.copy()
            elif self._index < len(self._chunk) and (self.phase == Phase.POLICY or leader_ready):
                action_index = self._index
                policy = self._chunk[self._index].copy()
                selected = policy.copy()
                self._index += 1
                source = "policy"
                self.phase = Phase.POLICY
        # Bound commanded tracking error per nominal tick; this is not a measured velocity guarantee.
        joint_speed = self.max_joint_speed
        if self.phase == Phase.HUMAN and self.max_manual_joint_speed is None:
            joint_speed = np.inf
        elif self.phase == Phase.HUMAN:
            joint_speed = self.max_manual_joint_speed
        limit = np.full(14, joint_speed * dt)
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
            self.phase == Phase.HUMAN and self.mode == Mode.HIL,
            policy is not None,
            self.phase in (Phase.HUMAN, Phase.HOLD, Phase.FAULT),
            tuple(self._pickup),
            self._active_request if policy is not None else None,
            action_index,
            self.phase == Phase.TAKEOVER,
        )
