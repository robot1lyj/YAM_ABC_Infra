"""Optional local smoke against condapi's actual ordinary WebSocket handler; no model/motors."""

import asyncio
import importlib.util
import os
import socket
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np
import pytest
from websockets.asyncio import server as ws_server

from yam_abc_reproduce.hil.policy import PlainPolicyClient


@pytest.mark.skipif(not os.environ.get("CONDAPI_ROOT"), reason="set CONDAPI_ROOT for cross-repo smoke")
def test_condapi_ordinary_wire_accepts_yam_observation():
    source = Path(os.environ["CONDAPI_ROOT"]) / "src/openpi/serving/websocket_policy_server.py"
    assert source.is_file(), source
    spec = importlib.util.spec_from_file_location("condapi_websocket_policy_server", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class FakePolicy:
        def infer(self, observation):
            assert set(observation) == {
                "observation.state", "observation.images.top_rgb",
                "observation.images.left_rgb", "observation.images.right_rgb", "prompt",
            }
            assert observation["observation.state"].shape == (14,)
            for role in ("top", "left", "right"):
                image = observation[f"observation.images.{role}_rgb"]
                assert image.dtype == np.uint8 and image.shape == (8, 8, 3)
            return {"actions": np.zeros((50, 14), dtype=np.float32)}

    policy_server = module.WebsocketPolicyServer(
        FakePolicy(), host="127.0.0.1", metadata={"robot_action_dim": 14, "action_horizon": 50},
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    stop = threading.Event()

    async def serve():
        async with ws_server.serve(
            policy_server._handler, "127.0.0.1", port,
            compression=None, max_size=None, process_request=module._health_check,
        ):
            await asyncio.to_thread(stop.wait)

    thread = threading.Thread(target=lambda: asyncio.run(serve()), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/healthz", timeout=0.2,
                ) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.01)
        else:
            pytest.fail("condapi test handler did not become ready")
        client = PlainPolicyClient(f"ws://127.0.0.1:{port}", timeout=1)
        try:
            assert client.metadata["robot_action_dim"] == 14
            observation = {"observation.state": np.zeros(14), "prompt": "test"}
            observation.update({
                f"observation.images.{role}_rgb": np.zeros((8, 8, 3), dtype=np.uint8)
                for role in ("top", "left", "right")
            })
            reply = client.infer(observation)
            assert reply["actions"].shape == (50, 14)
            assert reply["server_timing"]["rtc_used"] is False
            assert client.last_timing["pack_ms"] >= 0
        finally:
            client.close()
    finally:
        stop.set()
        thread.join(timeout=1)
