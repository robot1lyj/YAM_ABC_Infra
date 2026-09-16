import threading
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from yam_abc_reproduce.hil.policy_process import ProcessPolicyClient
from yam_abc_reproduce.hil.web import create_app


def test_policy_process_can_reload_without_replacing_device_owner():
    pytest.importorskip("openpi_client")
    from openpi_client import msgpack_numpy
    from websockets.sync.server import serve

    def handler(ws):
        packer = msgpack_numpy.Packer()
        ws.send(packer.pack({"action_horizon": 50}))
        while True:
            try:
                obs = msgpack_numpy.unpackb(ws.recv(timeout=2))
            except Exception:
                return
            ws.send(packer.pack({"actions": np.tile(obs["observation.state"], (50, 1))}))

    with serve(handler, "127.0.0.1", 0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = ProcessPolicyClient(
            f"ws://127.0.0.1:{server.socket.getsockname()[1]}", timeout=2
        )
        observation = {"observation.state": np.zeros(14), "prompt": "test"}
        try:
            assert client.infer(observation)["actions"].shape == (50, 14)
            old_pid = client._process.pid
            client.restart()
            assert client._process.pid != old_pid
            assert client.infer(observation)["actions"].shape == (50, 14)
        finally:
            client.close()
            server.shutdown()
            thread.join(timeout=2)


def test_policy_settings_api_validates_and_forwards_without_motor_calls():
    configured = []
    reloaded = []
    owner = SimpleNamespace(
        status={"phase": "hold"},
        configure_policy=lambda **settings: configured.append(settings),
        restart_policy=lambda: reloaded.append(True),
    )
    headers = {"X-YAM-Control": "1"}
    with TestClient(create_app(owner)) as client:
        assert client.post("/policy/settings", headers=headers, json={
            "fusion": "smooth", "smooth_steps": 4,
        }).status_code == 200
        assert client.post("/policy/settings", headers=headers, json={
            "fusion": "ensemble", "smooth_steps": 4,
        }).status_code == 422
        assert client.post("/policy/settings", headers=headers, json={
            "fusion": "smooth", "smooth_steps": 0,
        }).status_code == 422
        assert client.post("/policy/restart", headers=headers).status_code == 200
    assert configured == [{"fusion": "smooth", "smooth_steps": 4}]
    assert reloaded == [True]
