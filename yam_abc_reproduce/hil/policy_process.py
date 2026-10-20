"""Restartable Thor transport process; motor and safety state never cross this boundary."""

from __future__ import annotations

import multiprocessing as mp

from .process_pipe import close_pipe_process, receive_process_reply, start_pipe_process


def _serve(conn, url: str, timeout: float, rtc: bool):
    # Import inside the spawned child so policy wire-code updates take effect
    # without importing robot drivers or replacing the device owner.
    from ..resource_qos import place_on_cpus
    from .policy import PlainPolicyClient
    from .rtc_protocol import RtcPolicyClient

    place_on_cpus("POLICY")
    client = None
    try:
        try:
            client = (RtcPolicyClient if rtc else PlainPolicyClient)(url, timeout=timeout)
            conn.send(("ready", client.metadata, None))
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
                if rtc and isinstance(observation, dict) and observation.get("transport") == "human_rtc":
                    result = client.infer_human_rtc(observation["observation"], human_rtc=observation["human_rtc"])
                elif rtc:
                    if not isinstance(observation, tuple) or len(observation) not in (3, 4, 5):
                        raise ValueError("RTC transport requires observation, tick, prefix")
                    result = client.infer_rtc(
                        observation[0], target_start_tick=observation[1],
                        committed_actions=observation[2],
                        **({"pld": observation[3]} if len(observation) >= 4 else {}),
                        **({"human_rtc": observation[4]} if len(observation) == 5 else {}),
                    )
                else:
                    result = client.infer(observation)
                conn.send((result, client.last_timing, None))
            except Exception as exc:
                conn.send((getattr(exc, "response", None), None, f"{type(exc).__name__}: {exc}"))
                break  # One handshake per child; recovery explicitly creates another.
    finally:
        if client is not None:
            client.close()
        conn.close()


class ProcessPolicyClient:
    """Blocking client used only by PolicyWorker's background thread."""

    def __init__(self, url: str, timeout: float = 1.5, *, rtc: bool = False):
        self.url = url
        self.timeout = timeout
        self.rtc = rtc
        self.last_timing = None
        self.metadata = None
        self._context = mp.get_context("spawn")
        self._process = None
        self._conn = None
        self._failed = False

    def _start(self):
        self._process, self._conn = start_pipe_process(
            self._context, _serve, args=(self.url, self.timeout, self.rtc),
            name="thor-policy",
        )
        # Cold ARM64 module imports can exceed the network RPC timeout. This
        # wait runs in PolicyWorker, not the control thread; inference remains
        # disabled until the child has completed its Thor handshake.
        try:
            kind, metadata, error = receive_process_reply(
                self._process, self._conn, timeout=max(8.0, self.timeout + 1.0),
                label="policy process startup",
            )
            if kind != "ready":
                raise RuntimeError(error or "policy process startup failed")
        except Exception:
            self._failed = True
            self.close()
            raise
        self.metadata = metadata
        self._failed = False

    def infer(self, observation):
        return self._infer(observation)

    def infer_human_rtc(self, observation, *, human_rtc):
        return self._infer(dict(transport="human_rtc", observation=observation, human_rtc=human_rtc))

    def infer_rtc(self, observation, *, target_start_tick: int, committed_actions, pld=None, human_rtc=None):
        if human_rtc is not None:
            return self._infer((observation, target_start_tick, committed_actions, pld, human_rtc))
        return self._infer((observation, target_start_tick, committed_actions) if pld is None else
                           (observation, target_start_tick, committed_actions, pld))

    def _infer(self, observation):
        if self._failed:
            raise RuntimeError("policy process unavailable; restart it while HOLD")
        if self._process is None:
            self._start()
        if not self._process.is_alive():
            self._failed = True
            self.close()
            raise RuntimeError("policy process stopped; restart it while HOLD")
        try:
            self._conn.send(observation)
            result, self.last_timing, error = receive_process_reply(
                self._process, self._conn, timeout=self.timeout + 1.0,
                label="policy process response",
            )
            if error:
                from .policy import PolicyResponseError
                raise PolicyResponseError(error, result)
            return result
        except Exception:
            # Preserve raw PolicyResponseError traces for recording, but never
            # let a late response on this pipe satisfy another request.
            self._failed = True
            self.close()
            raise

    def restart(self):
        self.close()
        self._start()

    def set_rtc(self, enabled: bool):
        self.rtc = bool(enabled)
        self.restart()

    def close(self):
        close_pipe_process(self._process, self._conn)
        self._process = self._conn = None
        self.metadata = None
