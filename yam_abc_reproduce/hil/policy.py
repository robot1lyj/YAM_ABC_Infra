"""Ordinary policy RPC for baseline or asynchronous replanning. No RTC protocol."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from .core import Request


@dataclass
class Reply:
    token: Request
    actions: np.ndarray | None
    error: str | None = None
    worker_elapsed_ms: float | None = None
    server_timing: dict | None = None
    client_timing: dict | None = None


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
        self._restart = threading.Event()
        self._ready = threading.Event()
        self.restart_error = None
        if hasattr(client, "restart"):
            self._restart.set()
        else:
            self._ready.set()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit(self, token: Request, observation: dict[str, Any]) -> bool:
        # Called only by the single control owner; snapshots must be immutable.
        if self._busy.is_set() or self._stop.is_set() or not self._ready.is_set():
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

    def request_restart(self):
        """Restart the network owner off the control thread; old tokens remain epoch-checked."""
        if not hasattr(self.client, "restart"):
            raise ValueError("policy client does not support independent restart")
        self._ready.clear()
        self._restart.set()

    @property
    def ready(self):
        return self._ready.is_set()

    def _run(self):
        try:
            while not self._stop.is_set():
                if self._restart.is_set():
                    self._restart.clear()
                    try:
                        self.client.restart()
                        self.restart_error = None
                        self._ready.set()
                    except Exception as exc:
                        self.restart_error = str(exc)
                try:
                    token, observation = self._requests.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    started = time.monotonic()
                    response = self.client.infer(observation)
                    reply = Reply(
                        token, np.array(response["actions"], copy=True),
                        worker_elapsed_ms=(time.monotonic() - started) * 1000,
                        server_timing=response.get("server_timing"),
                        client_timing=getattr(self.client, "last_timing", None),
                    )
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
        self.last_timing = None
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
            started = time.monotonic()
            packed = self._packer.pack(observation)
            packed_at = time.monotonic()
            self._ws.send(packed)
            sent_at = time.monotonic()
            response = self._ws.recv(timeout=self.timeout)
            received_at = time.monotonic()
            if isinstance(response, str):
                raise RuntimeError(response)
            result = self._codec.unpackb(response)
            decoded_at = time.monotonic()
            self.last_timing = {
                "pack_ms": (packed_at - started) * 1000,
                "send_ms": (sent_at - packed_at) * 1000,
                "wait_response_ms": (received_at - sent_at) * 1000,
                "unpack_ms": (decoded_at - received_at) * 1000,
                "payload_bytes": len(packed),
            }
            return result
        except Exception:
            self.close()
            raise

    def close(self):
        self._ws.close()
