"""Replaceable action-chunk planner; no SDK, CAN, or motor authority here."""

from __future__ import annotations

import multiprocessing as mp
from time import monotonic

import numpy as np


def _snap_close_grippers(actions):
    """Temporary inference experiment: make small close targets decisive."""
    result = np.asarray(actions, dtype=np.float64).copy()
    grippers = result[:, [6, 13]]
    result[:, [6, 13]] = np.where(grippers < 0.3, 0.05, grippers)
    return result


def build_plan(mode, token, actions, previous, now, action_dt, max_action_age):
    """Pure planner entrypoint, also used by offline parity tests."""
    from .action_buffer import ActionBuffer, TimedChunk
    from .tda_buffer import TdaActionBuffer

    rows = np.asarray(actions, dtype=np.float64)
    if rows.shape != (50, 14) or not np.isfinite(rows).all():
        raise ValueError("policy response must be finite (50,14)")
    rows = rows.copy()
    rows[:, [6, 13]] = np.clip(rows[:, [6, 13]], 0.0, 1.0)
    origin = token.observed_at if token.observed_at is not None else token.created_at
    if mode == "raw":
        buffer = ActionBuffer(action_dt, max_action_age=max_action_age)
        if previous is not None and previous["kind"] == "clock":
            buffer.chunk = TimedChunk(
                previous["token"], previous["origin"], previous["first_index"],
                previous["actions"],
            )
        if not buffer.integrate(token, rows, origin, now):
            raise ValueError("policy action horizon already expired")
        return {
            "kind": "clock", "origin": origin,
            "first_index": buffer.chunk.first_index,
            "actions": buffer.chunk.actions,
            "trimmed_steps": buffer.last_trimmed_steps,
            "seam_max_rad": buffer.last_seam_max_rad,
            "seam_gripper_max": buffer.last_seam_gripper_max,
        }
    if mode == "tda_smooth":
        buffer = TdaActionBuffer(action_dt)
        if previous is not None and previous["kind"] == "queue":
            buffer._queue = [row.copy() for row in previous["actions"]]
            buffer._meta = list(previous["meta"])
        if not buffer.integrate(token, rows, origin, now) or not buffer._queue:
            raise ValueError("policy action queue is empty")
        return {
            "kind": "queue", "origin": origin, "first_index": 0,
            "actions": _snap_close_grippers(np.stack(buffer._queue)),
            "meta": buffer._meta,
            "based_on_consumed": (
                0 if previous is None else previous["consumed_total"]
            ),
            "trimmed_steps": buffer.last_trimmed_steps,
            "seam_max_rad": buffer.last_seam_max_rad,
        }
    if mode == "sync_hold":
        return {
            "kind": "queue", "origin": now, "first_index": 0,
            "actions": _snap_close_grippers(rows),
            "meta": [(token, index) for index in range(50)],
            "based_on_consumed": 0,
            "trimmed_steps": 0,
        }
    raise ValueError("unknown policy plan mode")


def _serve(conn):
    from ..resource_qos import place_on_cpus

    place_on_cpus("POLICY")
    try:
        conn.send(("ready", None))
        while True:
            try:
                request = conn.recv()
            except EOFError:
                break
            if request is None:
                break
            try:
                conn.send(("plan", build_plan(*request)))
            except Exception as exc:
                conn.send(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        conn.close()


class ProcessActionPlanner:
    """Called only from PolicyWorker's background thread, never a control tick."""

    def __init__(self, timeout: float = 1.0):
        self.timeout = timeout
        self._context = mp.get_context("spawn")
        self._process = None
        self._conn = None

    @property
    def alive(self):
        return self._process is not None and self._process.is_alive()

    def _start(self):
        parent, child = self._context.Pipe()
        process = self._context.Process(target=_serve, args=(child,), daemon=True,
                                        name="yam-action-planner")
        try:
            process.start()
        except Exception:
            parent.close()
            child.close()
            raise
        child.close()
        self._conn, self._process = parent, process
        if not parent.poll(max(8.0, self.timeout)):
            self.close()
            raise TimeoutError("action planner startup timeout")
        try:
            kind, error = parent.recv()
        except EOFError:
            self.close()
            raise RuntimeError("action planner exited during startup") from None
        if kind != "ready":
            self.close()
            raise RuntimeError(error or "action planner startup failed")

    def plan(self, mode, token, actions, previous, now, action_dt, max_action_age):
        if self._process is None or not self._process.is_alive():
            raise RuntimeError("action planner unavailable; restart while HOLD")
        try:
            self._conn.send((mode, token, actions, previous, now, action_dt, max_action_age))
            deadline = monotonic() + self.timeout
            while monotonic() < deadline:
                if self._conn.poll(min(0.02, max(0, deadline - monotonic()))):
                    kind, payload = self._conn.recv()
                    if kind == "error":
                        raise ValueError(payload)
                    return payload
                if not self._process.is_alive():
                    raise RuntimeError("action planner exited")
            raise TimeoutError("action planner response timeout")
        except (BrokenPipeError, EOFError, OSError):
            raise RuntimeError("action planner communication failed") from None

    def restart(self):
        self.close()
        self._start()

    def close(self):
        process, conn = self._process, self._conn
        self._process = self._conn = None
        if process is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=0.5)
        if conn is not None:
            conn.close()
