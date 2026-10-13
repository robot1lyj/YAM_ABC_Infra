"""Replaceable action-chunk planner; no SDK, CAN, or motor authority here."""

from __future__ import annotations

import multiprocessing as mp

import numpy as np

from .process_pipe import close_pipe_process, receive_process_reply, start_pipe_process


def build_plan(mode, token, actions, action_source="model"):
    """Plan model chunks or already submitted replay targets without mixing their transforms."""
    if mode != "sync_hold":
        raise ValueError("unknown policy plan mode; RTC uses its own committed-tick queue")

    rows = np.asarray(actions, dtype=np.float64)
    if rows.shape != (50, 14) or not np.isfinite(rows).all():
        raise ValueError("policy response must be finite (50,14)")
    rows = rows.copy()
    rows[:, [6, 13]] = np.clip(rows[:, [6, 13]], 0.0, 1.0)
    if action_source not in ("model", "recorded_replay"):
        raise ValueError("unknown planner action source")
    if action_source == "model":
        # Ordinary model-only experiment; replay and RTC preserve their targets.
        grippers = rows[:, [6, 13]]
        rows[:, [6, 13]] = np.where(grippers < 0.3, 0.1, grippers)
    return {
        "kind": "queue",
        "actions": rows,
        "meta": [(token, index) for index in range(50)],
    }


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
        self._process, self._conn = start_pipe_process(
            self._context, _serve, name="yam-action-planner")
        try:
            kind, error = receive_process_reply(
                self._process, self._conn, timeout=max(8.0, self.timeout),
                label="action planner startup")
            if kind != "ready":
                raise RuntimeError(error or "action planner startup failed")
        except Exception:
            self.close()
            raise

    def plan(self, mode, token, actions, action_source="model"):
        if self._process is None or not self._process.is_alive():
            raise RuntimeError("action planner unavailable; restart while HOLD")
        try:
            self._conn.send((mode, token, actions, action_source))
            kind, payload = receive_process_reply(
                self._process, self._conn, timeout=self.timeout, label="action planner response")
            if kind == "error":
                raise ValueError(payload)
            if kind != "plan":
                raise RuntimeError("invalid action planner response")
            return payload
        except TimeoutError:
            # Replies have no request IDs. Once a response misses its deadline,
            # this pipe must never serve another request: a late old plan could
            # otherwise be installed with that next request's provenance.
            self.close()
            raise
        except (BrokenPipeError, EOFError, OSError):
            self.close()
            raise RuntimeError("action planner communication failed") from None
        except RuntimeError:
            self.close()
            raise

    def restart(self):
        self.close()
        self._start()

    def close(self):
        close_pipe_process(self._process, self._conn)
        self._process = self._conn = None
