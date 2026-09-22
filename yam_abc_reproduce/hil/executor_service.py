"""Persistent SDK owner. Only this process may create/write/close four arms.

Local JSON/Unix protocol v1: explicit attach lease, sequenced absolute targets,
bounded age, execution acknowledgement. Losing the session freezes both pairs;
it never closes the SDK. Hardware release is a separate supported operation.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import queue
import socketserver
import threading
import time
import uuid
from pathlib import Path


class Executor:
    def __init__(self, factory, *, timeout=.25):
        self.factory, self.timeout = factory, timeout
        self.io = None
        self.lease = None
        self.seq = -1
        self.last_command = 0.
        self.frozen = None
        self.fault = None
        self.latched = False
        self.snapshot = None
        self.gripper_limits = [None, None]
        self.requests = queue.Queue(maxsize=8)
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.run, name="sdk-owner", daemon=True)

    def call(self, request, timeout=10):
        answer = queue.Queue(maxsize=1)
        self.requests.put_nowait((request, answer))
        result = answer.get(timeout=timeout)
        if "error" in result:
            raise ValueError(result["error"])
        return result

    def _read(self):
        q, h, buttons, ages = self.io.read()
        if max(ages) > self.timeout:
            raise ValueError("SDK state update stale")
        self.snapshot = dict(q=q.tolist(), leader=h.tolist(), buttons=buttons,
                             ages=ages, sampled_at=time.monotonic(),
                             limits=[x.tolist() for x in self.io._limits],
                             gripper_limits=self.gripper_limits,
                             control=self.io.leader_control_status())

    def freeze(self, *, revoke_lease=True):
        if revoke_lease:
            self.lease = None  # Revoke first, even if a physical write fails.
        if self.snapshot is not None:
            self.frozen = (self.snapshot["q"], self.snapshot["leader"])
        if self.frozen is not None:
            self._write(*self.frozen, manual=False, gain=.4, gravity=False)

    def _write(self, followers, leaders, *, manual, gain, gravity):
        followers = self.io.limit_policy_target(followers)
        leaders = self.io.limit_policy_target(leaders)
        stamps = {}
        for i, u in enumerate(self.io.units):
            if self.io.mock:
                if not manual:
                    self.io._mock_leaders[i*7:i*7+6] = leaders[i*7:i*7+6]
            else:
                u.agent.hil_leader_command(leaders[i*7:i*7+6], manual=manual, gain_scale=gain)
            stamps[f"{u.name}_leader"] = time.monotonic()
        self.io._manual = manual
        self.io._write_followers(followers, gravity=gravity)
        for u in self.io.units:
            stamps[f"{u.name}_follower"] = time.monotonic()
        return followers.tolist(), stamps

    def handle(self, r):
        now = time.monotonic()
        op = r.get("op")
        if r.get("version") != 1:
            raise ValueError("unsupported executor protocol")
        if op == "status":
            return dict(connected=self.io is not None, leased=self.lease is not None,
                        fault=self.fault, latched=self.latched, pid=os.getpid(), snapshot=self.snapshot)
        if op == "stop":
            self.latched = True
            self.freeze()
            return {"held": True, "latched": True}
        if op == "reset_stop":
            if self.fault:
                raise ValueError("hardware fault cannot be cleared by reset_stop")
            if self.io is not None:
                self._read()
            self.latched = False
            return {"held": True, "latched": False}  # Never restores an old lease.
        if op == "attach":
            if self.latched:
                raise ValueError("executor stop latched")
            if self.lease is not None:
                raise ValueError("another session owns the executor")
            if self.fault:
                raise ValueError("hardware fault requires supported release/reconnect")
            if self.io is None:
                self.io = self.factory()
                self.gripper_limits = [getattr(u.robot, "gripper_limits", lambda: None)()
                                       for u in self.io.units]
            self._read()
            self.freeze()
            self.lease, self.seq, self.last_command = uuid.uuid4().hex, -1, time.monotonic()
            return dict(lease=self.lease, snapshot=self.snapshot)
        if op == "release":
            if r.get("supported") is not True:
                raise ValueError("support all arms before release")
            if self.lease is not None and r.get("lease") != self.lease:
                raise ValueError("release requires current lease")
            self.lease = None
            errors = self.io.close() if self.io else []
            self.io = self.snapshot = self.frozen = None
            self.fault = None
            self.latched = False
            return dict(errors=errors)
        if not self.lease or r.get("lease") != self.lease:
            raise ValueError("expired executor lease; reconnect explicitly")
        if op == "hold":
            try:
                self.freeze(revoke_lease=False)
            except Exception as exc:
                self.lease = None
                self.fault = f"SDK hold failed: {exc}"
                raise
            self.last_command = time.monotonic()
            return {"held": True}
        if op == "detach":
            self.freeze()
            return {"held": True}
        if op != "apply":
            raise ValueError("unknown executor command")
        seq, created = r.get("seq"), r.get("created_at")
        if type(seq) is not int or seq <= self.seq:
            raise ValueError("old executor sequence")
        if not isinstance(created, (int, float)) or not math.isfinite(created) or not 0 <= now-created <= self.timeout:
            raise ValueError("expired executor command")
        gain = r.get("gain")
        if not isinstance(gain, (int, float)) or not math.isfinite(gain) or not 0 < gain <= 1:
            raise ValueError("invalid leader gain")
        if type(r.get("manual")) is not bool or type(r.get("gravity")) is not bool:
            raise ValueError("invalid control mode")
        # Validate both targets fully before the first actuator write.
        q = self.io.limit_policy_target(r["followers"])
        h = self.io.limit_policy_target(r["leaders"])
        try:
            target, stamps = self._write(q, h, manual=r["manual"], gain=gain, gravity=r["gravity"])
        except Exception as exc:
            self.fault = f"SDK write failed: {exc}"
            raise
        self.seq, self.last_command, self.frozen = seq, time.monotonic(), None
        return dict(seq=seq, submitted=target, stamps=stamps)

    def run(self):
        while not self.stopping.is_set():
            if self.io is not None:
                try:
                    self._read()
                    if self.lease and time.monotonic()-self.last_command > self.timeout:
                        self.freeze()
                except Exception as exc:
                    self.fault = str(exc)
                    try:
                        self.freeze()
                    except Exception:
                        pass  # Stale/disconnected hardware cannot promise a physical hold.
            try:
                request, answer = self.requests.get(timeout=.01)
            except queue.Empty:
                continue
            try:
                answer.put(self.handle(request))
            except Exception as exc:
                # Malformed/rejected commands cannot leave the old trajectory running.
                if request.get("op") == "apply" and request.get("lease") == self.lease:
                    try:
                        self.freeze()
                    except Exception as hold_exc:
                        self.fault = str(hold_exc)
                answer.put({"error": f"{type(exc).__name__}: {exc}"})
        if self.io is not None:
            self.io.close()  # Stopping this service, unlike the session, releases torque.


def serve(executor, path):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(15)
            try:
                data = self.rfile.readline(65537)
                if len(data) > 65536 or not data.endswith(b"\n"):
                    raise ValueError("invalid packet size")
                result = executor.call(json.loads(data))
            except Exception as exc:
                result = {"error": str(exc)}
            self.wfile.write(json.dumps(result, allow_nan=False).encode()+b"\n")

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Never unlink an existing socket: a second owner must fail, not steal it.
    with Server(str(path), Handler) as server:
        import signal
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
            signal.signal(signal.SIGINT, lambda *_: threading.Thread(target=server.shutdown).start())
        path.chmod(0o600)
        executor.thread.start()
        try:
            server.serve_forever()
        finally:
            executor.stopping.set()
            executor.thread.join()  # Do not remove the socket while an SDK owner lives.
            path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--station", required=True)
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()

    def factory():
        from ..config import build_station_config
        from ..resource_qos import place_on_cpus
        from ..runtime import build_arm_units
        from .run import prepare_station_can, validate_station
        from .station import StationIO
        cfg = build_station_config(args.station)
        validate_station(cfg, mock=args.mock, check_cameras=False)
        place_on_cpus("CONTROL")
        if not args.mock:
            prepare_station_can(cfg)
        return StationIO(build_arm_units(cfg, mock=args.mock), mock=args.mock)

    serve(Executor(factory), args.socket)


if __name__ == "__main__":
    main()
