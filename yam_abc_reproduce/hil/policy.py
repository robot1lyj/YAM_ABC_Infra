"""Bounded ordinary policy RPC. No RTC, action-prefix, blending or prefetch."""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Any

import numpy as np

from .core import Request


@dataclass
class Reply:
    token: Request
    actions: np.ndarray | None
    error: str | None = None


class PolicyWorker:
    """Single IO owner. Consumers validate epoch/age; never mutate Arbiter here.

    Exactly one pending/running/replied job until poll(). Network clients must
    enforce their own finite timeout. The control thread never joins on takeover.
    """

    def __init__(self, client):
        self.client = client
        self._requests = queue.Queue(maxsize=1)
        self._replies = queue.Queue(maxsize=1)
        self._busy = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit(self, token: Request, observation: dict[str, Any]) -> bool:
        # Called only by the single control owner; snapshots must be immutable.
        if self._busy.is_set() or self._stop.is_set():
            return False
        self._busy.set()
        self._requests.put_nowait((token, observation))
        return True

    def poll(self) -> Reply | None:
        try:
            reply = self._replies.get_nowait()
        except queue.Empty:
            return None
        self._busy.clear()
        return reply

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    token, observation = self._requests.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    response = self.client.infer(observation)
                    reply = Reply(token, np.array(response["actions"], copy=True))
                except Exception as exc:
                    reply = Reply(token, None, f"{type(exc).__name__}: {exc}")
                self._replies.put_nowait(reply)
        finally:
            close = getattr(self.client, "close", None)
            if close:
                close()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=0.1)


class PlainPolicyClient:
    """OpenPI wire format with bounded connect/receive and no proxy/reconnect loop.

    Close on timeout: an old response must never satisfy a subsequent request on
    the same socket. New connections and metadata validation belong to recovery.
    """

    def __init__(self, url: str, timeout: float = 2.0):
        from openpi_client import msgpack_numpy
        from websockets.sync.client import connect

        if timeout <= 0 or not np.isfinite(timeout):
            raise ValueError("finite positive timeout required")
        self.timeout = timeout
        self._codec = msgpack_numpy
        self._packer = msgpack_numpy.Packer()
        self._ws = connect(
            url,
            proxy=None,
            compression=None,
            max_size=32 * 1024 * 1024,
            open_timeout=timeout,
            close_timeout=0.1,
        )
        try:
            self.metadata = self._codec.unpackb(self._ws.recv(timeout=timeout))
        except Exception:
            self.close()
            raise

    def infer(self, observation):
        try:
            self._ws.send(self._packer.pack(observation))
            response = self._ws.recv(timeout=self.timeout)
            if isinstance(response, str):
                raise RuntimeError(response)
            return self._codec.unpackb(response)
        except Exception:
            self.close()
            raise

    def close(self):
        self._ws.close()
