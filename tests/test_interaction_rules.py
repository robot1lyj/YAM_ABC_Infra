from types import SimpleNamespace

import numpy as np
import pytest

from yam_abc_reproduce.hil.core import Arbiter, Mode, Phase
from yam_abc_reproduce.hil.interaction_rules import load_rules
from yam_abc_reproduce.hil.session import Session


def test_alignment_is_bounded_and_pause_cancels_it():
    a = Arbiter(Mode.HIL)
    q, h = np.zeros(14), np.zeros(14)
    h[0], h[6], h[13] = .6, .7, .8
    a.start(q)
    a.takeover(q, h)
    previous = h.copy()
    for i in range(15):
        d = a.step(q, previous, now=i/30, dt=1/30)
        np.testing.assert_array_equal(d.action, q)
        assert np.max(np.abs(d.leader_hold_target-previous)) <= .8/30 + 1e-8
        np.testing.assert_array_equal(d.leader_hold_target[[6, 13]], [.7, .8])
        previous = d.leader_hold_target.copy()
    d = Session(a).tick(q, previous, now=.6, dt=1/30, observation_id=1, event="hold")
    assert a._alignment is None and d.phase == Phase.HOLD
    np.testing.assert_allclose(d.leader_hold_target, previous)


def test_alignment_timeout_stops_assistance_but_allows_absolute_takeover():
    a = Arbiter(Mode.HIL)
    q, h = np.zeros(14), np.zeros(14)
    h[0] = .4
    a.start(q)
    a.takeover(q, h)
    for i in range(180):
        d = a.step(q, h, now=i/30, dt=1/30)
    assert d.phase == Phase.TAKEOVER and a._alignment["stopped"]
    assert "辅助对齐已停止" in a.alignment_error
    np.testing.assert_array_equal(d.leader_hold_target, h)
    a.manual_ready(q, h)
    assert a.phase == Phase.HUMAN and a._alignment is None
    d = a.step(q, h, now=7, dt=1/30)
    np.testing.assert_array_equal(d.action, h)


def test_button_during_alignment_reuses_absolute_teleoperation():
    a = Arbiter(Mode.HIL)
    q, h = np.zeros(14), np.zeros(14)
    h[0] = .9
    a.start(q)
    a.takeover(q, h)
    a.step(q, h, now=0, dt=1/30)
    q[0], h[0] = .03, .8
    a.manual_ready(q, h)
    assert a.phase == Phase.HUMAN and a._alignment is None
    d = a.step(q, h, now=.03, dt=1/30)
    np.testing.assert_allclose(d.action, h)
    h[0] += .02
    d = a.step(q, h, now=.06, dt=1/30)
    assert abs(d.action[0] - .82) < 1e-10
    teleop = Arbiter(Mode.TELEOP)
    teleop.start(q, h)
    other = teleop.step(q, h, now=.06, dt=1/30)
    np.testing.assert_array_equal(d.action, other.action)
    assert d.leader_manual == other.leader_manual


def test_stop_cancels_alignment_and_cannot_resume_old_target():
    a = Arbiter(Mode.HIL)
    q, h = np.zeros(14), np.zeros(14)
    h[0] = .4
    a.start(q)
    a.takeover(q, h)
    d = Session(a).tick(q, h, now=0, dt=1/30, observation_id=1, event="stop")
    assert d.phase == Phase.FAULT and a._alignment is None
    np.testing.assert_array_equal(d.action, q)


def test_handback_locks_until_explicit_resume_and_reload_preserves_state():
    a = Arbiter(Mode.HIL)
    q, h = np.zeros(14), np.full(14, .01)
    a.start(q)
    a.takeover(q, h)
    a.manual_ready(q, h)
    a.handback_hold(q, h)
    epoch = a.epoch
    a.interaction_rules = load_rules()
    for t in (.1, .2):
        d = a.step(q + .01, h + .02, now=t, dt=1/30)
        assert d.phase == Phase.HOLD and d.leader_freeze and not d.leader_manual
        np.testing.assert_array_equal(d.action, q)
        np.testing.assert_array_equal(d.leader_hold_target, h)
        assert a.request(1, t) is None and a.epoch == epoch
    a.resume_policy(q)
    assert a.phase == Phase.RESUME and a._leader_frozen is None


def test_replay_refusal_holds_without_fault_or_consumption():
    a = Arbiter(Mode.HIL, streaming=False)
    q = np.zeros(14)
    a.start(q)
    token = a.request(1, 0)
    reply = SimpleNamespace(token=token, error=None, actions=np.zeros((50, 14)),
                            worker_elapsed_ms=1, client_timing={},
                            server_timing={"source": "recorded_replay", "replay_refused": "align first"})
    worker = SimpleNamespace(poll=lambda: reply, planner_alive=True)
    s = Session(a, worker)
    s.replay_next_frame = 8
    d = s.tick(q, q, now=.1, dt=1/30, observation_id=1)
    assert d.phase == Phase.HOLD and d.leader_freeze
    assert a.fault_reason is None and s.replay_next_frame == 8
    assert s.notice == "align first"


def test_reload_rules_is_fresh_module_and_has_no_hardware():
    first, second = load_rules(), load_rules()
    assert first is not second and first.revision == second.revision
    assert first.button_event("hil", "human", True, True) == "handback_hold"


def test_intervention_start_cannot_bypass_pause_or_handback():
    a = Arbiter(Mode.HIL)
    q = np.zeros(14)
    a.start(q)
    a.takeover(q, q)
    s = Session(a)
    d = s.tick(q, q, now=.1, dt=1/30, observation_id=1, event="hold")
    assert d.leader_freeze and a.intervention_pending
    a.start(q, q)
    assert a.phase == Phase.HOLD
    a.resume_policy(q)
    assert a.phase == Phase.RESUME and not a.intervention_pending


@pytest.mark.parametrize("command", [{"delta": .01}, {"target": .15}])
def test_jog_available_in_all_paused_modes_but_not_during_intervention(command):
    import threading

    import pytest

    from yam_abc_reproduce.hil.run import Runtime
    r = object.__new__(Runtime)
    r.task_switching = False
    calls = []
    r.recorder = SimpleNamespace(recording=False)
    r.emergency = threading.Event()
    r.maintenance = SimpleNamespace(latched=False, state="idle")
    r.session = SimpleNamespace(arbiter=SimpleNamespace(intervention_pending=False))
    r.jog = SimpleNamespace(request=lambda *args, **kwargs: calls.append((args, kwargs)))
    for mode in Mode:
        r.status = {"mode": mode.value, "phase": "hold"}
        r.request_jog("left", 6, **command)
    assert len(calls) == 4
    r.session.arbiter.intervention_pending = True
    with pytest.raises(ValueError, match="介入"):
        r.request_jog("left", 6, **command)
    r.session.arbiter.intervention_pending = False
    r.recorder.recording = True
    with pytest.raises(ValueError):
        r.request_jog("left", 6, **command)
    r.recorder.recording = False
    r.emergency.set()
    with pytest.raises(ValueError):
        r.request_jog("left", 6, **command)
    assert len(calls) == 4


def test_runtime_reload_keeps_io_owner_and_locked_targets(tmp_path):
    from yam_abc_reproduce.camera.mock_camera import MockCamera
    from yam_abc_reproduce.camera.worker import CameraWorker
    from yam_abc_reproduce.config import StationConfig
    from yam_abc_reproduce.hil.recording import Recorder
    from yam_abc_reproduce.hil.run import Runtime
    from yam_abc_reproduce.hil.station import StationIO
    from yam_abc_reproduce.runtime import build_arm_units

    io = StationIO(build_arm_units(StationConfig(), mock=True), mock=True)
    rec = Recorder(tmp_path / "episode")
    cameras = [CameraWorker(MockCamera(r, r, width=32, height=32)) for r in ("top", "left", "right")]
    runtime = Runtime(io, cameras, None, rec)
    a = runtime.session.arbiter
    q = np.zeros(14)
    a.start(q)
    a.takeover(q, q)
    a.manual_ready(q, q)
    a.handback_hold(q, q)
    runtime.status = {"phase": "hold"}
    runtime.session.replay_next_frame = 17
    runtime.reload_interaction()
    try:
        for c in cameras:
            c.start()
        result = runtime.run(duration=.15)
        assert result["error"] is None
        assert runtime.io is io and runtime.status["phase"] == "hold"
        assert runtime.status["interaction_revision"] != "bundled"
        assert runtime.status["leader_locked"]
        assert runtime.session.replay_next_frame == 17
        np.testing.assert_array_equal(a._hold, q)
        assert a.interaction_rules is io.interaction_rules
    finally:
        rec.close()
        io.close()
        for c in cameras:
            c.stop()
