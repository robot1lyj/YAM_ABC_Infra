import threading
import time

import numpy as np
import pytest

from yam_abc_reproduce.hil.core import Arbiter, Mode, Phase
from yam_abc_reproduce.hil.policy import PolicyWorker
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
    a.toggle(q, q)
    assert a.phase == Phase.HUMAN
    assert not a.accept(old, np.tile(q, (50, 1)), 0.01)
    a.toggle(q, q)
    assert a.phase == Phase.RESUME
    token = a.request(2, 0.02)
    assert a.accept(token, np.tile(q, (50, 1)), 0.03)
    assert a.step(q, q, now=0.04, dt=0.03).source == "hold"
    assert a.step(q, q, now=0.05, dt=0.03, leader_ready=True).source == "policy"


def test_soft_pickup_and_joint_mismatch():
    q = pose(0.7)
    h = pose(0.1)
    a = Arbiter(Mode.TELEOP)
    a.start(q, h)
    d = a.step(q, h, now=0, dt=0.03)
    assert d.action[6] == 0.7
    h[[6, 13]] = 0.8
    d = a.step(q, h, now=0.03, dt=0.03)
    assert d.gripper_owned == (True, True)
    assert d.action[6] == pytest.approx(0.73)
    h[0] = 1
    a.start(q, h)
    assert a.phase == Phase.HOLD


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
        assert d.action[0] == pytest.approx(0.018)
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
        d = session.tick(**kw, now=0.03, event="toggle")
        assert d.phase == Phase.HUMAN
        assert not release.is_set()
        release.set()
        deadline = time.monotonic() + 1
        while worker._replies.empty() and time.monotonic() < deadline:
            time.sleep(0.001)
        d = session.tick(**kw, now=0.06)
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
