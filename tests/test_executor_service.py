import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from yam_abc_reproduce.config import StationConfig
from yam_abc_reproduce.hil.core import Arbiter, Mode
from yam_abc_reproduce.hil.executor_service import Executor
from yam_abc_reproduce.hil.remote_station import RemoteStationIO
from yam_abc_reproduce.hil.station import StationIO
from yam_abc_reproduce.runtime import build_arm_units


def test_lease_expiry_freezes_and_rejects_old_commands_without_closing_sdk():
    io = StationIO(build_arm_units(StationConfig(), mock=True), mock=True)
    owner = Executor(lambda: io, timeout=.04)
    owner.thread.start()
    def call(op, **kw):
        return owner.call(dict(version=1, op=op, **kw))
    try:
        lease = call("attach")["lease"]
        with pytest.raises(ValueError, match="another session"):
            call("attach")
        packet = dict(lease=lease, seq=1, created_at=time.monotonic(),
                      followers=[.1]*14, leaders=[.1]*14, gain=1., manual=False, gravity=False)
        assert call("apply", **packet)["seq"] == 1
        time.sleep(.08)
        status = call("status")
        assert status["connected"] and not status["leased"] and owner.io is io
        with pytest.raises(ValueError, match="expired executor lease"):
            call("apply", **packet)
        new = call("attach")["lease"]
        assert new != lease
        with pytest.raises(ValueError, match="expired executor lease"):
            call("detach", lease=lease)
        assert owner.lease == new
        call("stop")
        with pytest.raises(ValueError, match="latched"):
            call("attach")
        call("reset_stop")
        assert owner.lease is None  # Reset cannot resume pre-stop motion.
        assert owner.io is io
        with pytest.raises(ValueError, match="support"):
            call("release")
        call("release", supported=True)
        assert owner.io is None
    finally:
        owner.stopping.set()
        owner.thread.join(2)


def test_invalid_target_has_no_partial_write_and_revokes_lease():
    io = StationIO(build_arm_units(StationConfig(), mock=True), mock=True)
    owner = Executor(lambda: io)
    owner.thread.start()
    try:
        lease = owner.call(dict(version=1, op="attach"))["lease"]
        with pytest.raises(ValueError):
            owner.call(dict(version=1, op="apply", lease=lease, seq=1,
                            created_at=time.monotonic(), followers=[.5]*14, leaders=[float("nan")]*14,
                            gain=1., manual=False, gravity=False))
        assert owner.lease is None
        q, *_ = io.read()
        np.testing.assert_array_equal(q, np.zeros(14))
    finally:
        owner.stopping.set()
        owner.thread.join(2)


@pytest.mark.parametrize("kind", ["duplicate", "expired", "future"])
def test_command_freshness_is_enforced_by_owner(kind):
    io = StationIO(build_arm_units(StationConfig(), mock=True), mock=True)
    owner = Executor(lambda: io)
    owner.thread.start()
    try:
        lease = owner.call(dict(version=1, op="attach"))["lease"]
        packet = dict(version=1, op="apply", lease=lease, seq=1,
                      created_at=time.monotonic(), followers=[0.]*14, leaders=[0.]*14,
                      manual=False, gain=.4, gravity=False)
        owner.call(packet)
        packet = dict(packet, seq=1 if kind == "duplicate" else 2,
                      created_at=time.monotonic() + (1 if kind == "future" else -1 if kind == "expired" else 0))
        with pytest.raises(ValueError, match="sequence|expired"):
            owner.call(packet)
        assert owner.lease is None and owner.io is io
    finally:
        owner.stopping.set()
        owner.thread.join(2)


def test_real_unix_process_session_reconnect_keeps_owner_pid(tmp_path):
    path = tmp_path / "executor.sock"
    process = subprocess.Popen([sys.executable, "-m", "yam_abc_reproduce.hil.executor_service",
                                "--socket", str(path), "--station", "configs/station_hil.yaml", "--mock"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    io = again = None
    try:
        deadline = time.monotonic()+5
        while not path.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert path.exists()
        io = RemoteStationIO(path)
        pid = io._rpc({"op": "status"})["pid"]
        q, h, _, _ = io.read()
        a = Arbiter(Mode.HIL)
        decision = a.step(q, h, now=time.monotonic(), dt=1/30)
        submitted, stamps = io.apply(decision, q, h, dt=1/30)
        np.testing.assert_array_equal(submitted, q)
        assert len(stamps) == 4
        # HOLD follower jogging must survive the proxy; it must not freeze the
        # requested follower target while suppressing passive Leader release.
        decision.action[0] = .02
        submitted, _ = io.apply(decision, q, h, dt=1/30)
        assert submitted[0] == .02
        assert io.close() == []
        again = RemoteStationIO(path)
        assert again._rpc({"op": "status"})["pid"] == pid
        assert again._rpc({"op": "status"})["connected"]
        again.release_on_close = True
        assert again.close() == []
        child = subprocess.Popen([sys.executable, "-c",
            "from yam_abc_reproduce.hil.remote_station import RemoteStationIO; "
            f"io=RemoteStationIO({str(path)!r}); print('attached', flush=True); "
            "import time; time.sleep(10)"], stdout=subprocess.PIPE, text=True)
        assert child.stdout.readline().strip() == "attached"
        child.kill()
        child.wait(timeout=2)
        time.sleep(.35)
        third = RemoteStationIO(path)
        try:
            assert third._rpc({"op": "status"})["pid"] == pid
            assert third._rpc({"op": "status"})["connected"]
            from yam_abc_reproduce.camera.mock_camera import MockCamera
            from yam_abc_reproduce.camera.worker import CameraWorker
            from yam_abc_reproduce.hil.recording import Recorder
            from yam_abc_reproduce.hil.run import Runtime
            cameras = [CameraWorker(MockCamera(r, r, width=32, height=32))
                       for r in ("top", "left", "right")]
            rec = Recorder(tmp_path / "episode")
            try:
                for c in cameras:
                    c.start()
                runtime = Runtime(third, cameras, None, rec, mode="teleop")
                result = runtime.run(duration=.15)
                assert result["error"] is None
                assert result["phase"] == "hold"
                assert third._rpc({"op": "status"})["pid"] == pid
            finally:
                rec.close()
                for c in cameras:
                    c.stop()
        finally:
            third.release_on_close = True
            third.close()
    finally:
        if io:
            io.close()
        if again:
            again.close()
        process.terminate()
        process.wait(timeout=5)


def test_recording_hold_keeps_remote_lease_and_control_alive(tmp_path):
    from yam_abc_reproduce.hil.recording import RecordingSession
    from yam_abc_reproduce.hil.run import Runtime

    path = tmp_path / "executor.sock"
    process = subprocess.Popen(
        [sys.executable, "-m", "yam_abc_reproduce.hil.executor_service",
         "--socket", str(path), "--station", "configs/station_hil.yaml", "--mock"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    io = recorder = runtime = thread = None
    try:
        deadline = time.monotonic() + 5
        while not path.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert path.exists()
        io = RemoteStationIO(path)
        lease = io.lease
        recorder = RecordingSession(tmp_path / "failed-recording", mode="teleop")
        recorder.error = "injected recording failure"
        cameras = [SimpleNamespace(role=role, history=lambda: []) for role in ("top", "left", "right")]
        runtime = Runtime(io, cameras, None, recorder, mode="teleop")
        thread = threading.Thread(target=runtime.run, kwargs={"duration": .2}, daemon=True)
        thread.start()
        thread.join(2)
        assert not thread.is_alive()
        assert runtime.status["phase"] == "hold"
        assert runtime.status["error"] is None
        assert runtime.status["recording_error"] == "injected recording failure"
        assert runtime.status["tick"] >= 3
        assert io.lease == lease and io._rpc({"op": "status"})["leased"]

        q, leader, *_ = io.read()
        decision = runtime.session.arbiter.step(q, leader, now=time.monotonic(), dt=1/30)
        io.apply(decision, q, leader, dt=1/30)
        assert io.close() == []
        state = io._rpc({"op": "status"})
        assert state["connected"] and not state["leased"]
    finally:
        if runtime is not None:
            runtime.stopping.set()
        if thread is not None:
            thread.join(2)
        if recorder is not None:
            recorder.close("aborted")
        if io is not None:
            io.close()
        process.terminate()
        process.wait(timeout=5)


def test_hold_rejects_foreign_lease_and_revokes_on_write_failure():
    io = StationIO(build_arm_units(StationConfig(), mock=True), mock=True)
    owner = Executor(lambda: io)
    try:
        lease = owner.handle(dict(version=1, op="attach"))["lease"]
        with pytest.raises(ValueError, match="expired executor lease"):
            owner.handle(dict(version=1, op="hold", lease="wrong"))
        assert owner.lease == lease

        def fail(*args, **kwargs):
            raise RuntimeError("injected CAN write failure")

        owner._write = fail
        with pytest.raises(RuntimeError, match="CAN write failure"):
            owner.handle(dict(version=1, op="hold", lease=lease))
        assert owner.lease is None and "SDK hold failed" in owner.fault
    finally:
        io.close()
