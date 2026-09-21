import threading
import time

import numpy as np
import pytest

from yam_abc_reproduce.hil.core import Arbiter, Mode, Phase
from yam_abc_reproduce.hil.policy import PolicyWorker


def test_transport_can_be_configured_after_worker_creation_without_auto_connect():
    import threading

    class Client:
        url = ""

        def __init__(self):
            self.connected = threading.Event()
            self.calls = []

        def restart(self):
            self.calls.append(self.url)
            self.connected.set()

        def close(self):
            pass

    client = Client()
    worker = PolicyWorker(client, auto_connect=False)
    try:
        assert not client.connected.wait(0.15)
        assert not worker.ready
        worker.request_source("ws://localhost:8000")
        assert client.connected.wait(2)
        assert worker._ready.wait(2)
        assert client.calls == ["ws://localhost:8000"]
    finally:
        worker.close()
from yam_abc_reproduce.hil.session import Session
from yam_abc_reproduce.hil.snapshots import Frame, FrameBuffers


def pose(grip=0.5):
    q = np.zeros(14)
    q[[6, 13]] = grip
    return q


def test_takeover_invalidates_inflight_and_requires_new_response():
    a = Arbiter(Mode.HIL)
    q = pose()
    a.start(q)
    old = a.request(1, 0)
    a.takeover(q, q)
    assert a.phase == Phase.TAKEOVER
    assert not a.accept(old, np.tile(q, (50, 1)), 0.01)
    a.step(q, q, now=0.011, dt=0.03)
    a.step(q, q, now=0.012, dt=0.03)
    a.manual_ready(q, q)
    a.resume_policy(q)
    assert a.phase == Phase.RESUME
    token = a.request(2, 0.02)
    assert a.accept(token, np.tile(q, (50, 1)), 0.03)
    assert a.step(q, q, now=0.04, dt=0.03).source == "policy"
    assert a.step(q, q, now=0.05, dt=0.03, leader_ready=True).source == "policy"


def test_soft_pickup_and_absolute_identity_mapping():
    q = pose(0.7)
    h = pose(0.1)
    q[[0, 7]] = [0.45, -0.6]
    h[[0, 7]] = [-0.4, 0.3]
    a = Arbiter(Mode.TELEOP)
    a.start(q, h)
    d = a.step(q, h, now=0, dt=0.03)
    assert d.phase == Phase.HUMAN
    np.testing.assert_allclose(d.action[[0, 7]], h[[0, 7]])
    assert d.action[6] == 0.7

    # Subsequent motion remains an absolute one-to-one leader mapping.
    h[[0, 7]] += [0.01, -0.02]
    d = a.step(q, h, now=0.015, dt=0.03)
    np.testing.assert_allclose(d.action[[0, 7]], h[[0, 7]])

    h[[6, 13]] = 0.8
    d = a.step(q, h, now=0.03, dt=0.03)
    assert d.gripper_owned == (True, True)
    assert d.action[6] == pytest.approx(0.8)


def test_manual_and_policy_targets_are_not_feedback_slew_clipped():
    q = pose()
    h = q.copy()
    a = Arbiter(Mode.COLLECT)
    a.start(q, h)
    h[0] = 1.0
    manual = a.step(q, h, now=0.03, dt=0.03)
    assert manual.action[0] == pytest.approx(1.0)

    a.change_mode(Mode.INFERENCE, q)
    a.start(q)
    token = a.request(1, 0.04)
    actions = np.tile(q, (50, 1))
    actions[:, 0] = 1.0
    assert a.accept(token, actions, 0.05)
    policy = a.step(q, q, now=0.06, dt=0.03, leader_ready=True)
    assert policy.action[0] == pytest.approx(1.0)
    np.testing.assert_allclose(policy.action, policy.selected_action)


def test_normal_chunks_do_not_prefetch():
    a = Arbiter(Mode.INFERENCE, execute_steps=2)
    q = pose()
    a.start(q)
    token = a.request(1, 0)
    actions = np.tile(q, (50, 1))
    actions[:, 0] = 0.5
    assert a.accept(token, actions, 0.01)
    assert a.request(2, 0.02) is None
    for t in (0.03, 0.06):
        d = a.step(q, q, now=t, dt=0.03, leader_ready=True)
        assert d.action[0] == pytest.approx(0.5)
    assert a.request(3, 0.07) is not None


@pytest.mark.parametrize("reason", ["timeout", "stale", "missed_tick"])
def test_control_holds_on_unusable_inputs(reason):
    a = Arbiter(Mode.HIL)
    q = pose()
    a.start(q)
    a.request(1, 0)
    d = a.step(
        q,
        q,
        now=0.6 if reason == "timeout" else 0.02,
        dt=0.2 if reason == "missed_tick" else 0.03,
        observation_fresh=reason != "stale",
    )
    assert d.phase in (Phase.HOLD, Phase.FAULT)
    assert not d.policy_valid


def test_slow_network_does_not_block_manual_event():
    entered, release = threading.Event(), threading.Event()

    class Client:
        def infer(self, obs):
            entered.set()
            assert release.wait(2)
            return {"actions": np.tile(pose(), (50, 1))}

    worker = PolicyWorker(Client())
    session = Session(Arbiter(Mode.HIL), worker)
    q = pose()
    kw = dict(state=q, leader=q, dt=0.03, observation_id=1, observation={})
    try:
        session.tick(**kw, now=0, event="start")
        assert entered.wait(1)
        d = session.tick(**kw, now=0.03, event="takeover")
        assert d.phase == Phase.TAKEOVER
        assert not release.is_set()
        release.set()
        deadline = time.monotonic() + 1
        while worker._replies.empty() and time.monotonic() < deadline:
            time.sleep(0.001)
        d = session.tick(**kw, now=0.06, event="manual_ready")
        assert d.source == "human" and not d.policy_valid
    finally:
        release.set()
        worker.close()


def test_software_frame_pairing_rejects_stale_and_retains_nearby_frames():
    buffers = FrameBuffers()
    im = np.zeros((2, 2, 3), dtype=np.uint8)
    for role in ("top", "left", "right"):
        buffers.publish(role, Frame(1, 1.0, im))
    buffers.publish("top", Frame(2, 1.08, im))
    assert buffers.snapshot(1.09)["top"].sequence == 1
    assert buffers.snapshot(1.3) is None
    with pytest.raises(ValueError):
        buffers.publish("left", Frame(1, 1.1, im))


def test_bad_response_and_old_response_do_not_execute():
    a = Arbiter(Mode.INFERENCE)
    q = pose()
    a.start(q)
    token = a.request(1, 0)
    with pytest.raises(ValueError):
        a.accept(token, np.zeros((50, 32)), 0.01)
    assert not a.accept(token, np.tile(q, (50, 1)), 0.6)
    assert a.phase == Phase.HOLD


def test_openpi_wire_protocol_round_trip():
    pytest.importorskip("openpi_client")
    from openpi_client import msgpack_numpy
    from websockets.sync.server import serve

    from yam_abc_reproduce.hil.policy import PlainPolicyClient

    def handler(ws):
        packer = msgpack_numpy.Packer()
        ws.send(packer.pack({"action_horizon": 50}))
        obs = msgpack_numpy.unpackb(ws.recv(timeout=1))
        assert obs["prompt"] == "test"
        ws.send(packer.pack({"actions": np.tile(obs["observation.state"], (50, 1))}))

    with serve(handler, "127.0.0.1", 0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = PlainPolicyClient(f"ws://127.0.0.1:{server.socket.getsockname()[1]}")
        try:
            out = client.infer({"observation.state": pose(), "prompt": "test"})
            assert client.metadata["action_horizon"] == 50
            assert out["actions"].shape == (50, 14)
        finally:
            client.close()
            server.shutdown()
            thread.join(1)


def test_takeover_freezes_then_uses_relative_leader_motion_and_does_not_toggle_back():
    q, h = pose(), pose()
    h[[0, 7]] = 0.13
    a = Arbiter(Mode.HIL)
    a.start(q)
    token = a.request(1, 0)
    a.accept(token, np.tile(q, (50, 1)), 0.01)
    a.step(q, h, now=0.02, dt=0.03, leader_ready=True)
    a.takeover(q, h)
    freeze = a.step(q, h, now=0.03, dt=0.03)
    assert freeze.source == "hold" and freeze.leader_freeze
    np.testing.assert_array_equal(freeze.action, q)
    waiting = a.step(q + .01, h + .01, now=1, dt=0.03)
    assert waiting.leader_freeze and waiting.source == "hold"
    np.testing.assert_array_equal(waiting.action, q)
    assert 0 < waiting.leader_hold_target[0] < h[0]
    a.manual_ready(q, h)
    assert a.phase == Phase.TAKEOVER  # An early button press cannot unlock.
    for i in range(40):
        h = a._leader_frozen.copy()
        a.step(q, h, now=1+i*.03, dt=.03)
    a.manual_ready(q, h)
    manual = a.step(q, h, now=1.03, dt=0.03)
    assert manual.source == "human" and not manual.leader_freeze
    np.testing.assert_allclose(manual.action, q)
    h[[0, 7]] += 0.01
    a.takeover(q, h)  # repeated keyboard input cannot hand back
    manual = a.step(q, h, now=0.09, dt=0.03)
    np.testing.assert_allclose(manual.action[[0, 7]], 0.01)
    assert a.request(2, 0.09) is None
    a.start(q, h)  # ordinary start cannot bypass explicit hand-back either
    assert a.phase == Phase.HUMAN
    a.resume_policy(q)
    assert a.phase == Phase.RESUME
