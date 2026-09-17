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


def test_policy_transport_switches_to_trained_rtc_in_child_only():
    pytest.importorskip("openpi_client")
    from openpi_client import msgpack_numpy
    from websockets.sync.server import serve

    def handler(ws):
        packer = msgpack_numpy.Packer()
        ws.send(packer.pack({"rtc_mode": "trained", "rtc_max_delay_steps": 10,
                             "action_horizon": 50, "action_dim": 14,
                             "action_dt_s": 1 / 30}))
        while True:
            try:
                payload = msgpack_numpy.unpackb(ws.recv(timeout=2))
            except Exception:
                return
            rows = np.zeros((50, 14))
            is_rtc = payload.get("type") == "infer"
            if is_rtc:
                prefix = payload["rtc"]["committed_actions"]
                rows[:len(prefix)] = prefix
            ws.send(packer.pack({"actions": rows,
                                 "server_timing": {"rtc_used": is_rtc}}))

    with serve(handler, "127.0.0.1", 0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = ProcessPolicyClient(
            f"ws://127.0.0.1:{server.socket.getsockname()[1]}", timeout=2
        )
        image = np.zeros((4, 6, 3), dtype=np.uint8)
        obs = {"observation.state": np.zeros(14), "prompt": "test",
               **{f"observation.images.{role}_rgb": image
                  for role in ("top", "left", "right")}}
        try:
            assert client.infer(obs)["actions"].shape == (50, 14)
            old_pid = client._process.pid
            client.set_rtc(True)
            assert client._process.pid != old_pid
            prefix = np.zeros((8, 14))
            prefix[:, 0] = .2
            result = client.infer_rtc(obs, target_start_tick=100,
                                      committed_actions=prefix)
            assert result["server_timing"]["rtc_used"] is True
            np.testing.assert_allclose(result["actions"][:8], prefix)
            client.set_rtc(False)
            assert client.infer(obs)["server_timing"]["rtc_used"] is False
        finally:
            client.close()
            server.shutdown()
            thread.join(timeout=2)


def test_policy_settings_api_validates_and_forwards_without_motor_calls():
    configured = []
    reloaded = []
    planner_reloaded = []
    owner = SimpleNamespace(
        status={"phase": "hold"},
        configure_policy=lambda **settings: configured.append(settings),
        restart_policy=lambda: reloaded.append(True),
        restart_planner=lambda: planner_reloaded.append(True),
    )
    headers = {"X-YAM-Control": "1"}
    with TestClient(create_app(owner)) as client:
        assert client.post("/policy/settings", headers=headers, json={
            "fusion": "tda_smooth",
        }).status_code == 200
        assert client.post("/policy/settings", headers=headers, json={
            "fusion": "rtc",
            "rtc_delay_steps": 9,
        }).status_code == 200
        assert client.post("/policy/settings", headers=headers, json={
            "fusion": "rtc", "rtc_delay_steps": 11,
        }).status_code == 422
        assert client.post("/policy/settings", headers=headers, json={
            "fusion": "ensemble",
        }).status_code == 422
        assert client.post("/policy/settings", headers=headers, json={
            "fusion": "smooth",
        }).status_code == 422
        assert client.post("/policy/settings", headers=headers, json={
            "fusion": "raw",
        }).status_code == 422
        assert client.post("/policy/restart", headers=headers).status_code == 200
        assert client.post("/policy/planner/restart", headers=headers).status_code == 200
    assert configured == [
        {"fusion": "tda_smooth"},
        {"fusion": "rtc", "rtc_delay_steps": 9},
    ]
    assert reloaded == [True]
    assert planner_reloaded == [True]
