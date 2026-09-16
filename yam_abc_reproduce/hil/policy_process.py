"""Restartable Thor transport process; motor and safety state never cross this boundary."""

from __future__ import annotations

import multiprocessing as mp
import time


def _serve(conn, url: str, timeout: float):
    # Import inside the spawned child so policy wire-code updates take effect
    # without importing robot drivers or replacing the device owner.
    from ..resource_qos import place_on_cpus
    from .policy import PlainPolicyClient

    place_on_cpus("POLICY")
    client = None
    try:
        try:
            client = PlainPolicyClient(url, timeout=timeout)
            conn.send(("ready", None, None))
        except Exception as exc:
            conn.send(("error", None, f"{type(exc).__name__}: {exc}"))
            return
        while True:
            try:
                observation = conn.recv()
            except EOFError:
                break
            if observation is None:
                break
            try:
                if client is None:
                    client = PlainPolicyClient(url, timeout=timeout)
                result = client.infer(observation)
                conn.send((result, client.last_timing, None))
            except Exception as exc:
                if client is not None:
                    client.close()
                    client = None
                conn.send((None, None, f"{type(exc).__name__}: {exc}"))
    finally:
        if client is not None:
            client.close()
        conn.close()


class ProcessPolicyClient:
    """Blocking client used only by PolicyWorker's background thread."""

    def __init__(self, url: str, timeout: float = 1.5):
        self.url = url
        self.timeout = timeout
        self.last_timing = None
        self._context = mp.get_context("spawn")
        self._process = None
        self._conn = None

    def _start(self):
        parent, child = self._context.Pipe()
        process = self._context.Process(
            target=_serve, args=(child, self.url, self.timeout), daemon=True,
            name="thor-policy",
        )
        try:
            process.start()
        except Exception:
            parent.close()
            child.close()
            raise
        child.close()
        self._conn, self._process = parent, process
        if not parent.poll(self.timeout + 1.0):
            self.close()
            raise TimeoutError("policy process startup timeout")
        try:
            kind, _, error = parent.recv()
        except EOFError:
            self.close()
            raise RuntimeError("policy process exited during startup") from None
        if kind != "ready":
            self.close()
            raise RuntimeError(error or "policy process startup failed")

    def infer(self, observation):
        if self._process is None:
            self._start()
        if not self._process.is_alive():
            raise RuntimeError("policy process stopped; restart it while HOLD")
        try:
            self._conn.send(observation)
            # Includes child startup, OpenPI handshake, and response. This wait
            # occurs exclusively in PolicyWorker, never the motor control loop.
            deadline = time.monotonic() + self.timeout + 1.0
            while time.monotonic() < deadline:
                if self._conn.poll(min(0.05, max(0, deadline - time.monotonic()))):
                    result, self.last_timing, error = self._conn.recv()
                    if error:
                        raise RuntimeError(error)
                    return result
                if not self._process.is_alive():
                    raise RuntimeError("policy process exited")
            raise TimeoutError("policy process response timeout")
        except (BrokenPipeError, EOFError, OSError):
            raise RuntimeError("policy process communication failed") from None

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
