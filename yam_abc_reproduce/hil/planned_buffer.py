"""Small, algorithm-neutral action plan consumed by the device owner.

The replaceable planner process builds plans.  This buffer only advances an
already validated plan on the controller clock; it never waits for that process.
"""

from __future__ import annotations

from math import floor
from threading import Lock

import numpy as np


class PlannedActionBuffer:
    def __init__(self, action_dt: float, *, max_action_age: float, fusion: str):
        self.action_dt = action_dt
        self.max_action_age = max_action_age
        self.fusion = fusion
        self._lock = Lock()
        self._plan = None
        self._cursor = 0
        self._consumed_total = 0
        self.last_selection = None
        self.last_trimmed_steps = None
        self.last_seam_max_rad = None
        self.last_seam_gripper_max = None

    @property
    def chunk(self):
        with self._lock:
            return self if self._plan is not None else None

    def clear(self):
        with self._lock:
            self._plan = None
            self._cursor = 0
            self._consumed_total = 0
            self.last_selection = None
            self.last_trimmed_steps = None
            self.last_seam_max_rad = None
            self.last_seam_gripper_max = None

    def install(self, plan: dict, token) -> bool:
        rows = np.asarray(plan["actions"], dtype=np.float64)
        if rows.ndim != 2 or rows.shape[1] != 14 or len(rows) == 0:
            raise ValueError("planner returned no 14D actions")
        if not np.isfinite(rows).all():
            raise ValueError("planner returned nonfinite actions")
        rows = rows.copy()
        rows[:, [6, 13]] = np.clip(rows[:, [6, 13]], 0.0, 1.0)
        kind = plan["kind"]
        if kind not in ("clock", "queue"):
            raise ValueError("unknown planner timeline")
        origin = float(plan["origin"])
        first_index = int(plan.get("first_index", 0))
        if not np.isfinite(origin) or first_index < 0:
            raise ValueError("invalid planner timeline")
        meta = plan.get("meta")
        if kind == "queue" and (meta is None or len(meta) != len(rows)):
            raise ValueError("invalid planner provenance")
        with self._lock:
            if kind == "queue":
                based_on = int(plan["based_on_consumed"])
                elapsed = self._consumed_total - based_on
                if elapsed < 0:
                    raise ValueError("planner queue snapshot is from the future")
                if elapsed >= len(rows):
                    return False
                rows = rows[elapsed:].copy()
                meta = meta[elapsed:]
            self._plan = {
                "kind": kind, "origin": origin, "first_index": first_index,
                "actions": rows, "meta": meta, "token": token,
            }
            self._cursor = 0
            self.last_selection = None
            self.last_trimmed_steps = plan.get("trimmed_steps")
            self.last_seam_max_rad = plan.get("seam_max_rad")
            self.last_seam_gripper_max = plan.get("seam_gripper_max")
            return True

    def snapshot(self):
        """Copy the unconsumed queue, or clock plan, for background replanning."""
        with self._lock:
            if self._plan is None:
                return None
            plan = self._plan
            start = self._cursor if plan["kind"] == "queue" else 0
            return {
                "kind": plan["kind"], "origin": plan["origin"],
                "first_index": plan["first_index"],
                "actions": plan["actions"][start:].copy(),
                "meta": None if plan["meta"] is None else plan["meta"][start:],
                "token": plan["token"],
                "consumed_total": self._consumed_total,
            }

    def _index(self, now: float):
        plan = self._plan
        if plan["kind"] == "queue":
            return self._cursor
        return floor((now - plan["origin"]) / self.action_dt + 1e-9) - plan["first_index"]

    def current(self, now: float):
        with self._lock:
            self.last_selection = None
            if self._plan is None:
                return None
            plan = self._plan
            offset = self._index(now)
            if offset < 0 or offset >= len(plan["actions"]):
                return None
            if plan["kind"] == "queue":
                token, index = plan["meta"][offset]
                self._cursor += 1
                self._consumed_total += 1
                self.last_selection = {
                    "fusion": self.fusion, "request_id": token.request_id,
                    "model_index": index,
                }
            else:
                index = plan["first_index"] + offset
                token = plan["token"]
                target_at = plan["origin"] + index * self.action_dt
                source = {
                    "epoch": token.epoch, "request_id": token.request_id,
                    "observed_at": plan["origin"], "model_index": index,
                }
                self.last_selection = {
                    "fusion": self.fusion, "target_at": target_at,
                    "joint_sources": [{**source, "weight": 1.0}],
                    "gripper_source": source,
                }
            return plan["actions"][offset].copy(), index, token

    def remaining(self, now: float) -> int:
        with self._lock:
            if self._plan is None:
                return 0
            return max(0, len(self._plan["actions"]) - max(0, self._index(now)))

    def seconds_to_expiry(self, now: float) -> float:
        with self._lock:
            if self._plan is None:
                return 0.0
            plan = self._plan
            if plan["kind"] == "queue":
                return max(0.0, (len(plan["actions"]) - self._cursor) * self.action_dt)
            end = min(
                plan["origin"] + (plan["first_index"] + len(plan["actions"]))
                * self.action_dt,
                plan["origin"] + self.max_action_age,
            )
            return max(0.0, end - now)
