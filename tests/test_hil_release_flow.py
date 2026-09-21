"""Formal HIL episode boundaries, using simulated arms and real local storage."""

import threading
import time

import numpy as np
import pytest

from yam_abc_reproduce.camera.mock_camera import MockCamera
from yam_abc_reproduce.camera.worker import CameraWorker
from yam_abc_reproduce.config import StationConfig
from yam_abc_reproduce.hil.core import Phase
from yam_abc_reproduce.hil.policy import PolicyWorker
from yam_abc_reproduce.hil.recording import RecordingSession
from yam_abc_reproduce.hil.run import MockPolicy, Runtime
from yam_abc_reproduce.hil.station import StationIO
from yam_abc_reproduce.hil.storage import read_rows
from yam_abc_reproduce.runtime import build_arm_units


@pytest.fixture
def station(tmp_path):
    io = StationIO(build_arm_units(StationConfig(), mock=True), mock=True)
    cameras = [CameraWorker(MockCamera(r, r, width=32, height=32))
               for r in ("top", "left", "right")]
    worker = PolicyWorker(MockPolicy())
    recorder = RecordingSession(tmp_path / "session")
    runtime = Runtime(io, cameras, worker, recorder, mode="hil")
    for camera in cameras:
        camera.start()
    thread = threading.Thread(target=runtime.run, kwargs={"duration": 10}, daemon=True)
    thread.start()
    def wait(phase):
        end = time.monotonic() + 4
        while runtime.status.get("phase") != phase and time.monotonic() < end:
            time.sleep(.01)
        assert runtime.status.get("phase") == phase, runtime.status
    time.sleep(.15)
    yield runtime, recorder, wait
    runtime.event("quit")
    thread.join(5)
    for camera in cameras:
        camera.stop()
    worker.close()
    io.close()
    assert not thread.is_alive()


@pytest.mark.parametrize("pause", [False, True])
def test_intervention_handback_preserves_or_restarts_recording(station, pause):
    runtime, recorder, wait = station
    runtime.event("start")
    wait("policy")
    runtime.event("takeover")
    wait("takeover")
    with pytest.raises(ValueError, match="介入"):
        runtime.event("mode:collect")
    with pytest.raises(ValueError, match="介入"):
        runtime.event("start")
    # Inject exactly the event produced by right handle 1, never a motor command.
    runtime.events.put(("manual_ready", time.monotonic()))
    wait("human")
    time.sleep(.15)
    if pause:
        runtime.event("hold")
    else:
        runtime.events.put(("handback_hold", time.monotonic()))
    wait("hold")
    assert recorder.recording is not pause
    runtime.event("resume_policy")
    wait("policy")
    assert recorder.recording and recorder.error is None
    time.sleep(.15)
    runtime.event("hold")
    wait("hold")
    end = time.monotonic() + 4
    while recorder.saving and time.monotonic() < end:
        time.sleep(.02)
    assert len(recorder.episodes) == (2 if pause else 1)
    assert all(e["steps"] > 0 for e in recorder.episodes)
    rows = list(read_rows(recorder.path / recorder.episodes[-1]["path"]))
    assert any(row["source"] == "policy" for row in rows)
    assert not any(row["phase"] == "takeover" for row in rows)


def test_failed_episode_start_cannot_resume_policy(station, monkeypatch):
    runtime, recorder, wait = station
    runtime.event("start")
    wait("policy")
    runtime.event("takeover")
    wait("takeover")
    runtime.event("hold")
    wait("hold")
    frozen = runtime.session.arbiter._leader_frozen.copy()
    monkeypatch.setattr(recorder, "start_episode", lambda: None)
    runtime.event("resume_policy")
    time.sleep(.25)
    assert runtime.session.arbiter.phase == Phase.HOLD
    assert runtime.recording_error
    assert not recorder.recording
    np.testing.assert_array_equal(runtime.session.arbiter._leader_frozen, frozen)


def test_confirmed_mode_change_ends_intervention_and_holds(station):
    runtime, recorder, wait = station
    runtime.event("start")
    wait("policy")
    runtime.event("takeover")
    wait("takeover")
    runtime.event("end_intervention:hil")
    wait("hold")
    assert not runtime.session.arbiter.intervention_pending
    assert runtime.session.arbiter._leader_frozen is not None
    assert not recorder.recording
