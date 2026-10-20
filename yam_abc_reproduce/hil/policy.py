"""Single-flight policy worker for ordinary blocks and explicit RTC commitments.

Transport and optional planning run off the control tick. The session remains
the authority for epoch validation, action acceptance and recovery to HOLD.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import numpy as np

from .core import Request
from .rtc_timeline import RtcCommitment


def validate_policy_url(url):
    """Shared by initial connection and both live source-switch entry points."""
    parsed = urlparse(url)
    if parsed.scheme not in ("ws", "wss") or not parsed.hostname:
        raise ValueError("来源地址需为 ws:// 或 wss://")


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
    pld: dict | None = None
    pld_request: dict | None = None
    recording_session: str | None = None
    request_context: dict | None = None
    human_rtc_request: dict | None = None
    human_rtc: dict | None = None


@dataclass(frozen=True)
class RtcJob:
    observation: dict[str, Any]
    commitment: RtcCommitment
    pld: dict | None = None
    recording_session: str | None = None
    request_context: dict | None = None
    human_rtc: dict | None = None


@dataclass(frozen=True)
class HumanRtcJob:
    observation: dict[str, Any]
    human_rtc: dict
    recording_session: str | None = None
    request_context: dict | None = None


def reply_record(reply, *, now, discarded):
    """One representation for normal ticks and post-HOLD cancellation audit."""
    return dict(token=reply.token.__dict__, received_at=now, discarded=discarded,
        error=reply.error, worker_elapsed_ms=reply.worker_elapsed_ms,
        server_timing=reply.server_timing, client_timing=reply.client_timing,
        actions=reply.actions if reply.actions is not None and np.isfinite(reply.actions).all() else None,
        pld=getattr(reply, "pld", None), pld_request=getattr(reply, "pld_request", None),
        recording_session=getattr(reply, "recording_session", None),
        request_context=getattr(reply, "request_context", None),
        human_rtc_request=getattr(reply, "human_rtc_request", None),
        human_rtc=getattr(reply, "human_rtc", None))


class PolicyResponseError(ValueError):
    """A rejected reply whose raw trace must survive transport validation."""

    def __init__(self, message, response):
        super().__init__(message)
        self.response = response


class PolicyWorker:
    """Single IO owner. Consumers validate epoch/age; never mutate Arbiter here.

    Exactly one pending/running/replied job until poll(). Network clients must
    enforce their own finite timeout. The control thread never joins on takeover.
    """

    def __init__(self, client, *, planner=None, auto_connect=True):
        self.client = client
        self.planner = planner
        self._requests = queue.Queue(maxsize=1)
        self._replies = queue.Queue(maxsize=1)
        self._busy = threading.Event()
        self._stop = threading.Event()
        self.close_errors = []
        self._restart = threading.Event()
        self._requested_rtc: bool | None = None
        self._requested_url = None
        self._restart_planner = threading.Event()
        self._ready = threading.Event()
        self.transport_error = None
        self.planner_restart_error = None
        self._client_ready = not hasattr(client, "restart")
        self._planner_ready = planner is None
        if not self._client_ready and auto_connect:
            self._restart.set()
        if not self._planner_ready and not getattr(client, "rtc", False):
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

    def mark_failed(self, error: str, *, planner: bool = False):
        """Latch a failed boundary until its explicit restart validates it again."""
        if planner:
            self._planner_ready = False
            self.planner_restart_error = str(error)
        else:
            self._client_ready = False
            self.transport_error = str(error)
        self._update_ready()

    @property
    def restart_error(self):
        return "; ".join(x for x in (self.transport_error, self.planner_restart_error) if x) or None

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
                        if self.planner is not None:
                            if getattr(self.client, "rtc", False):
                                self.planner.close()
                                self._planner_ready = False
                                self.planner_restart_error = None
                                self._restart_planner.clear()
                            elif not self.planner_alive:
                                self._restart_planner.set()
                        self.transport_error = None
                        self._client_ready = True
                    except Exception as exc:
                        self.transport_error = str(exc)
                    self._update_ready()
                if self._restart_planner.is_set():
                    self._restart_planner.clear()
                    try:
                        if getattr(self.client, "rtc", False):
                            self.planner.close()
                        else:
                            self.planner.restart()
                        self.planner_restart_error = None
                        self._planner_ready = not getattr(self.client, "rtc", False)
                    except Exception as exc:
                        self.planner_restart_error = str(exc)
                    self._update_ready()
                try:
                    token, observation = self._requests.get(timeout=0.05)
                except queue.Empty:
                    continue
                planning = False
                response = None
                request_pld = observation.pld if isinstance(observation, RtcJob) else None
                guidance = getattr(observation, "human_rtc", None)
                audit = dict(pld_request=request_pld, human_rtc_request=guidance,
                    recording_session=getattr(observation, "recording_session", None),
                    request_context=getattr(observation, "request_context", None))
                try:
                    started = time.monotonic()
                    if isinstance(observation, HumanRtcJob):
                        response = self.client.infer_human_rtc(observation.observation,
                            human_rtc=observation.human_rtc)
                    elif isinstance(observation, RtcJob):
                        response = self.client.infer_rtc(
                            observation.observation,
                            target_start_tick=observation.commitment.observation_tick,
                            committed_actions=observation.commitment.actions,
                            **({"pld": observation.pld} if observation.pld is not None else {}),
                            **({"human_rtc": guidance} if guidance is not None else {}),
                        )
                    else:
                        response = self.client.infer(observation)
                    actions = np.array(response["actions"], copy=True)
                    if actions.shape != (50, 14) or not np.isfinite(actions).all():
                        raise ValueError("policy response must be finite (50,14)")
                    if guidance is not None:
                        from .human_rtc import validate_confirmation
                        validate_confirmation(guidance, response)
                    from .pld import validate_reply
                    validate_reply(request_pld, response, token=token,
                        observation=observation.observation if isinstance(observation, (RtcJob, HumanRtcJob)) else observation)
                    server_timing = response.get("server_timing")
                    if server_timing is not None and not isinstance(server_timing, dict):
                        raise ValueError("policy server_timing must be a mapping")
                    plan = None
                    if self.planner is not None and not isinstance(observation, (RtcJob, HumanRtcJob)):
                        planning = True
                        action_source = (
                            "recorded_replay"
                            if (server_timing or {}).get("source") == "recorded_replay"
                            else "model"
                        )
                        plan = self.planner.plan(
                            "sync_hold", token, actions, action_source,
                        )
                    reply = Reply(
                        token, actions,
                        worker_elapsed_ms=(time.monotonic() - started) * 1000,
                        server_timing=server_timing,
                        client_timing=getattr(self.client, "last_timing", None),
                        plan=plan,
                        pld=response.get("pld"), human_rtc=response.get("human_rtc"), **audit,
                    )
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    self.mark_failed(error, planner=planning)
                    response = getattr(exc, "response", response)
                    reply = Reply(token, None, error, planner_error=planning,
                        pld=response.get("pld") if isinstance(response, dict) else None,
                        human_rtc=response.get("human_rtc") if isinstance(response, dict) else None, **audit)
                self._replies.put_nowait(reply)
        finally:
            from .cleanup import close_resources

            resources = []
            close = getattr(self.client, "close", None)
            if close:
                resources.append(("policy transport", close))
            if self.planner is not None:
                resources.append(("action planner", self.planner.close))
            self.close_errors = close_resources(resources)

    def close(self):
        self._stop.set()
        # Called during session teardown, never from a control tick. Allow the
        # bounded cold handshake/RPC to finish before its owner releases pipes.
        self._thread.join(timeout=max(10.0, getattr(self.client, "timeout", 1.5) + 2.0))
        if self._thread.is_alive():
            raise RuntimeError("policy worker did not stop; cleanup remains pending")
        if self.close_errors:
            raise RuntimeError("; ".join(self.close_errors))


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
