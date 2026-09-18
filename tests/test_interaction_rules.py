from types import SimpleNamespace

import numpy as np

from yam_abc_reproduce.hil.core import Arbiter, Mode, Phase
from yam_abc_reproduce.hil.interaction_rules import load_rules
from yam_abc_reproduce.hil.session import Session


def test_handback_locks_until_explicit_resume_and_reload_preserves_state():
    a = Arbiter(Mode.HIL)
    q, h = np.zeros(14), np.full(14, .1)
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
