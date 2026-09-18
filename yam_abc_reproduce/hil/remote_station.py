"""Session-side target planning with no SDK objects; execution is acknowledged.

The existing StationIO mapping is reused with command collectors. Only the
complete validated four-arm packet crosses the private Unix socket.
"""
import json
import socket
import time
from types import SimpleNamespace

import numpy as np

from .station import StationIO


class RemoteStationIO(StationIO):
    def __init__(self, path, *, mock=False, **settings):
        if settings.get("policy_trajectory_hz", 0):
            raise ValueError("executor protocol v1 requires direct 30 Hz targets")
        self.path = str(path)
        self.lease = None
        self.sequence = 0
        self.release_on_close = False
        self.closed = False
        self.packet = {}
        self.held = None
        initial = self._rpc({"op": "attach"}, timeout=30)
        self.lease = initial["lease"]
        self.cache = initial["snapshot"]
        units = []
        for index, name in enumerate(("left", "right")):
            def follower(target, *, i=index):
                self.packet[f"follower_{i}"] = np.asarray(target).copy()

            def gravity(target, *, i=index):
                follower_target = np.asarray(target).copy()
                self.packet[f"follower_{i}"] = follower_target
                self.packet["gravity"] = True

            def leader(target, *, manual, gain_scale, i=index):
                self.packet[f"leader_{i}"] = np.asarray(target).copy()
                self.packet["manual"], self.packet["gain"] = manual, gain_scale

            robot = SimpleNamespace(
                get_joint_pos=lambda i=index: np.asarray(self.cache["q"][i*7:i*7+7]),
                joint_limits=lambda i=index: np.asarray(self.cache["limits"][i]),
                gripper_limits=lambda i=index: self.cache["gripper_limits"][i],
                command_joint_pos=follower, gravity_compensate=gravity,
            )
            units.append(SimpleNamespace(name=name, robot=robot,
                                         agent=SimpleNamespace(hil_leader_command=leader)))
        # Collectors use the real (non-mock) apply path, but never read hardware.
        super().__init__(units, mock=False, **settings)
        self.remote_mock = mock
        self._read_pool.shutdown()
        self._read_pool = None

    def _rpc(self, request, *, timeout=.2):
        request.update(version=1)
        if self.lease:
            request["lease"] = self.lease
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(self.path)
            sock.sendall(json.dumps(request, allow_nan=False).encode()+b"\n")
            with sock.makefile("rb") as stream:
                raw = stream.readline(65537)
            if len(raw) > 65536 or not raw.endswith(b"\n"):
                raise RuntimeError("invalid executor response")
            result = json.loads(raw)
        if "error" in result:
            raise RuntimeError(result["error"])
        return result

    def read(self):
        status = self._rpc({"op": "status"})
        if status["fault"]:
            raise RuntimeError(status["fault"])
        self.cache = status["snapshot"]
        if self.cache is None:
            raise RuntimeError("executor is disconnected")
        age = max(0, time.monotonic()-self.cache["sampled_at"])
        return (np.asarray(self.cache["q"]), np.asarray(self.cache["leader"]),
                self.cache["buttons"], [a+age for a in self.cache["ages"]])

    def leader_control_status(self):
        return self.cache["control"]

    def apply(self, decision, q, leader, **kwargs):
        self.packet = {"gravity": False}
        super().apply(decision, q, leader, **kwargs)
        followers = np.concatenate([self.packet[f"follower_{i}"] for i in (0, 1)])
        leaders = np.asarray(leader).copy()
        for i in (0, 1):
            leaders[i*7:i*7+6] = self.packet[f"leader_{i}"]
        # A restarted session starts HOLD, not gravity-comp release. Explicit
        # maintenance retains its own mode; RESUME alignment is not overridden.
        if str(decision.phase) == "hold" and not kwargs.get("gravity") and kwargs.get("maintenance_leader") is None:
            if self.held is None:
                self.held = np.asarray(leader).copy()
            # Follower jog targets still execute; only passive Leader release is
            # suppressed. Explicit frozen targets remain higher priority.
            if not decision.leader_freeze:
                leaders = self.held
                self.packet.update(manual=False, gain=.4)
        else:
            self.held = None
        self.sequence += 1
        reply = self._rpc(dict(op="apply", seq=self.sequence, created_at=time.monotonic(),
                               followers=followers.tolist(), leaders=leaders.tolist(),
                               manual=bool(self.packet["manual"]), gain=float(self.packet["gain"]),
                               gravity=self.packet["gravity"]))
        if reply["seq"] != self.sequence:
            raise RuntimeError("executor acknowledgement mismatch")
        return np.asarray(reply["submitted"]), reply["stamps"]

    def hold(self):
        if self.closed or self.lease is None:
            return []
        try:
            self._rpc({"op": "detach"})
            self.lease = None
            return []
        except Exception as exc:
            return [str(exc)]  # Owner watchdog independently holds on lease loss.

    def close(self):
        if self.closed:
            return []
        try:
            if self.release_on_close:
                return self._rpc({"op": "release", "supported": True}, timeout=10)["errors"]
            return self.hold()
        finally:
            self.closed = True
