"""Failure-to-recovery contracts: real workers and mock IO, never real motors."""

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from yam_abc_reproduce.hil.core import Arbiter, Mode, Phase
from yam_abc_reproduce.hil.data_session import (
    DataChange,
    RecorderReplacement,
    paused_data_session,
    recover_recording,
)
from yam_abc_reproduce.hil.policy import PolicyWorker, Reply
from yam_abc_reproduce.hil.recording import RecordingSession
from yam_abc_reproduce.hil.recording_service import RemoteRecordingSession
from yam_abc_reproduce.hil.run import Runtime
from yam_abc_reproduce.hil.session import Session


def wait_until(predicate, timeout=4):
    end = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < end, "condition did not become true"
        time.sleep(.01)


def cameras():
    return [SimpleNamespace(role=r, history=lambda: []) for r in ("top", "left", "right")]


class IO:
    mock = True

    def __init__(self):
        self.writes = 0
        self.phases = []

    def read(self):
        q = np.zeros(14)
        return q, q.copy(), [[False, False]] * 2, [0.] * 4

    def apply(self, decision, *args, **kwargs):
        self.writes += 1
        self.phases.append(decision.phase)
        return decision.action.copy(), {}

    def hold(self):
        return []


def test_transport_failure_hold_restart_still_requires_explicit_start():
    class Client:
        failed = False

        def restart(self):
            pass

        def infer(self, observation):
            if not self.failed:
                self.failed = True
                raise ConnectionError("model disconnected")
            return {"actions": np.zeros((50, 14))}

        def close(self):
            pass

    worker = PolicyWorker(Client())
    a = Arbiter(Mode.INFERENCE, policy_fusion="sync_hold")
    session = Session(a, worker)
    q = np.zeros(14)

    def tick(event=None):
        return session.tick(q, q, now=time.monotonic(), dt=1/30, observation_id=1,
                            observation={"observation.state": q}, event=event)

    try:
        wait_until(lambda: worker.ready)
        tick("start")
        old = a.pending
        wait_until(lambda: (tick(), session.policy_error)[1])
        assert a.phase == Phase.HOLD and a.fault_reason is None
        assert not worker.ready and old.epoch != a.epoch
        tick("start")
        assert a.phase == Phase.HOLD
        worker.request_restart()
        session.begin_policy_recovery()
        wait_until(lambda: worker.ready)
        tick()
        assert session.policy_error is None and a.phase == Phase.HOLD
        tick("start")
        wait_until(lambda: tick().source == "policy")
    finally:
        worker.close()


@pytest.mark.parametrize("plan", [None, {}, {"actions": np.zeros((50, 14))}])
def test_malformed_plan_is_local_policy_fault(plan):
    q = np.zeros(14)
    a = Arbiter(Mode.INFERENCE, external_planner=True)
    a.start(q)
    token = a.request(1, 0)
    reply = Reply(token, np.zeros((50, 14)), plan=plan)
    failures = []
    worker = SimpleNamespace(poll=lambda: reply, ready=True, planner_alive=True,
                             mark_failed=lambda reason, **kw: failures.append(kw))
    session = Session(a, worker)
    decision = session.tick(q, q, now=.01, dt=1/30, observation_id=1)
    assert decision.phase == Phase.HOLD
    assert a.fault_reason is None and session.policy_error
    assert failures == [{"planner": True}]


@pytest.mark.parametrize("recorder_type", [RecordingSession, RemoteRecordingSession])
def test_recording_recovery_keeps_control_ticks_and_retains_old_session(tmp_path, recorder_type):
    recorder = recorder_type(tmp_path / "failed", mode="collect", metadata={"task": "test"})
    io = IO()
    runtime = Runtime(io, cameras(), None, recorder, mode="collect")
    thread = threading.Thread(target=runtime.run, daemon=True)
    thread.start()
    try:
        wait_until(lambda: io.writes > 1)
        if recorder_type is RemoteRecordingSession:
            recorder.process.kill()  # Only this test's recording child.
            recorder.process.join(2)
        else:
            recorder.error = "simulated disk error"
        wait_until(lambda: runtime.recording_error is not None)
        assert runtime.status["phase"] == "hold"
        old_path = recorder.path
        before = io.writes
        with paused_data_session(runtime, recovering=True) as change:
            output = recover_recording(runtime, change)
            assert change.committed
        assert io.writes > before  # Spawn/drain did not block control.
        assert runtime.io is io and runtime.recorder is not recorder
        assert output.exists() and output != old_path and old_path.exists()
        wait_until(lambda: runtime.status.get("recording_error") is None)
        assert runtime.status["phase"] == "hold" and not runtime.recorder.recording
        assert set(io.phases) == {Phase.HOLD}
        assert not recorder._thread.is_alive()
        runtime.recorder.start_episode()
        runtime.recorder.stop_episode("unknown")
        assert not runtime.recorder.error
    finally:
        runtime.stopping.set()
        thread.join(4)
        runtime.recorder.close("aborted")
        if runtime.recorder is recorder:
            recorder.close("aborted")
    assert not thread.is_alive()


def test_failed_candidate_does_not_replace_or_clear_latched_error(tmp_path, monkeypatch):
    recorder = RecordingSession(tmp_path / "failed", mode="collect")
    recorder.error = "original writer failed"
    runtime = Runtime(IO(), cameras(), None, recorder, mode="collect")
    runtime.recording_error = recorder.error
    original = RecordingSession._write_manifest

    def fail_new(self, **kwargs):
        if self is recorder:
            original(self, **kwargs)
        else:
            self.error = "new storage unavailable"

    monkeypatch.setattr(RecordingSession, "_write_manifest", fail_new)
    try:
        with pytest.raises(RuntimeError, match="new storage unavailable"):
            recover_recording(runtime, DataChange())
        assert runtime.recorder is recorder
        assert runtime.recording_error == "original writer failed"
    finally:
        recorder.close("aborted")


def test_cancelled_recorder_exchange_cannot_install_late():
    old, new = object(), object()
    runtime = SimpleNamespace(recorder=old, task_switching=True,
                              session=SimpleNamespace(arbiter=SimpleNamespace(phase=Phase.HOLD)))
    exchange = RecorderReplacement(old, new, cancelled=True)
    exchange.apply(runtime)
    assert exchange.done.is_set() and not exchange.accepted and runtime.recorder is old


def test_sdk_stale_still_latches_hardware_fault(tmp_path):
    io = IO()
    io.read = lambda: (np.zeros(14), np.zeros(14), [[False, False]]*2, [2.]*4)
    recorder = RecordingSession(tmp_path / "hardware-fault")
    runtime = Runtime(io, cameras(), None, recorder)
    try:
        runtime.run(duration=.05)
        assert runtime.status["phase"] == "fault"
        assert "SDK state update stale" in runtime.status["error"]
        assert not io.writes
    finally:
        recorder.close("aborted")


def test_recording_failure_can_explicitly_end_intervention_without_motion(tmp_path):
    recorder = RecordingSession(tmp_path / "intervention", mode="hil")
    runtime = Runtime(IO(), cameras(), None, recorder)
    q = np.zeros(14)
    a = runtime.session.arbiter
    a.start(q)
    a.takeover(q, q)
    a.manual_ready(q, q)
    runtime._recording_failed(q, "disk unavailable")
    assert a.intervention_pending
    try:
        runtime.event("end_intervention:teleop")
        runtime.run(duration=.08)
        assert not a.intervention_pending and a.phase == Phase.HOLD
        assert a.mode == Mode.TELEOP and runtime.recording_error
    finally:
        recorder.close("aborted")
