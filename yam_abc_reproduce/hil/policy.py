"""Ordinary policy RPC for baseline or asynchronous replanning. No RTC protocol."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from .core import Request
from .rtc_timeline import RtcCommitment


@dataclass
class Reply:
    token: Request
    actions: np.ndarray | None
    error: str | None = None
    worker_elapsed_ms: float | None = None
    server_timing: dict | None = None
    client_timing: dict | None = None
    plan: dict | None = None
    planner_error: bool = False


@dataclass(frozen=True)
class RtcJob:
    observation: dict[str, Any]
    commitment: RtcCommitment


class PolicyWorker:
    """Single IO owner. Consumers validate epoch/age; never mutate Arbiter here.

    Exactly one pending/running/replied job until poll(). Network clients must
    enforce their own finite timeout. The control thread never joins on takeover.
    """

    def __init__(self, client, *, planner=None, auto_connect=True):
        self.client = client
        self.planner = planner
        self.plan_context = None
        self._requests = queue.Queue(maxsize=1)
        self._replies = queue.Queue(maxsize=1)
        self._busy = threading.Event()
        self._stop = threading.Event()
        self._restart = threading.Event()
        self._requested_rtc: bool | None = None
        self._requested_url = None
        self._restart_planner = threading.Event()
        self._ready = threading.Event()
        self.restart_error = None
        self._client_ready = not hasattr(client, "restart")
        self._planner_ready = planner is None
        if not self._client_ready and auto_connect:
            self._restart.set()
        if not self._planner_ready:
            self._restart_planner.set()
        self._update_ready()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _update_ready(self):
        if self._client_ready and (
            self._planner_ready or getattr(self.client, "rtc", False)
        ):
            self._ready.set()
        else:
            self._ready.clear()

    def submit(self, token: Request, observation: dict[str, Any]) -> bool:
        # Called only by the single control owner; snapshots must be immutable.
        if self._busy.is_set() or self._stop.is_set() or not self.ready:
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
        self._client_ready = False
        self._update_ready()
        self._restart.set()

    def request_transport_mode(self, rtc: bool):
        if not hasattr(self.client, "set_rtc"):
            raise ValueError("policy transport cannot switch RTC mode")
        self._requested_rtc = bool(rtc)
        self._client_ready = False
        self._update_ready()
        self._restart.set()

    def request_planner_restart(self):
        if self.planner is None:
            raise ValueError("action planner process is not enabled")
        self._planner_ready = False
        self._update_ready()
        self._restart_planner.set()

    def request_source(self, url):
        self._requested_url = url
        self._client_ready = False
        self._update_ready()
        self._restart.set()

    @property
    def ready(self):
        return self._ready.is_set() and (
            self.planner_alive or getattr(self.client, "rtc", False)
        )

    @property
    def planner_alive(self):
        return self.planner is None or (
            self._planner_ready and getattr(self.planner, "alive", True)
        )

    def _run(self):
        try:
            while not self._stop.is_set():
                if self._restart.is_set():
                    self._restart.clear()
                    try:
                        requested_rtc = self._requested_rtc
                        self._requested_rtc = None
                        requested_url = self._requested_url
                        self._requested_url = None
                        if requested_url is not None:
                            self.client.url = requested_url
                        if requested_rtc is None:
                            self.client.restart()
                        else:
                            self.client.set_rtc(requested_rtc)
                        self.restart_error = None
                        self._client_ready = True
                    except Exception as exc:
                        self.restart_error = str(exc)
                    self._update_ready()
                if self._restart_planner.is_set():
                    self._restart_planner.clear()
                    try:
                        self.planner.restart()
                        self.restart_error = None
                        self._planner_ready = True
                    except Exception as exc:
                        self.restart_error = str(exc)
                    self._update_ready()
                try:
                    token, observation = self._requests.get(timeout=0.05)
                except queue.Empty:
                    continue
                planning = False
                try:
                    started = time.monotonic()
                    if isinstance(observation, RtcJob):
                        response = self.client.infer_rtc(
                            observation.observation,
                            target_start_tick=observation.commitment.observation_tick,
                            committed_actions=observation.commitment.actions,
                        )
                    else:
                        response = self.client.infer(observation)
                    actions = np.array(response["actions"], copy=True)
                    plan = None
                    if self.planner is not None and not isinstance(observation, RtcJob):
                        planning = True
                        if self.plan_context is None:
                            raise RuntimeError("action planner context is not attached")
                        mode, previous, action_dt, max_action_age = self.plan_context()
                        plan = self.planner.plan(
                            mode, token, actions, previous, time.monotonic(),
                            action_dt, max_action_age,
                        )
                    reply = Reply(
                        token, actions,
                        worker_elapsed_ms=(time.monotonic() - started) * 1000,
                        server_timing=response.get("server_timing"),
                        client_timing=getattr(self.client, "last_timing", None),
                        plan=plan,
                    )
                except Exception as exc:
                    reply = Reply(token, None, f"{type(exc).__name__}: {exc}",
                                  planner_error=planning)
                self._replies.put_nowait(reply)
        finally:
            close = getattr(self.client, "close", None)
            if close:
                close()
            if self.planner is not None:
                self.planner.close()

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
            observation = dict(observation)
            if self.metadata.get("source") != "recorded_replay":
                observation.pop("_replay_cursor", None)
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
