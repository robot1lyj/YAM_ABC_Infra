"""RTC transport contract tests; no camera, robot, or Thor model is constructed."""

import threading

import numpy as np
import pytest

from yam_abc_reproduce.hil.rtc_protocol import RtcPolicyClient, build_rtc_request


def observation():
    image = np.zeros((4, 6, 3), dtype=np.uint8)
    return {
        "observation.state": np.zeros(14),
        "observation.images.top_rgb": image,
        "observation.images.left_rgb": image,
        "observation.images.right_rgb": image,
        "prompt": "pick up the block",
    }


def test_rtc_envelope_has_explicit_target_tick_and_absolute_prefix():
    prefix = np.zeros((3, 14), dtype=np.float32)
    prefix[:, 0] = [0.1, 0.2, 0.3]
    request = build_rtc_request(
        observation(), target_start_tick=42, committed_actions=prefix, max_delay_steps=10
    )
    assert request["type"] == "infer"
    assert request["rtc"]["delay_steps"] == 3
    assert request["rtc"]["observation_policy_tick"] == 42
    assert request["rtc"]["target_start_tick"] == 42
    assert request["rtc"]["committed_start_tick"] == 42
    np.testing.assert_array_equal(request["rtc"]["committed_actions"], prefix)
    prefix[0, 0] = 99
    assert request["rtc"]["committed_actions"][0, 0] == pytest.approx(0.1)
    first = build_rtc_request(
        observation(), target_start_tick=0, committed_actions=[], max_delay_steps=10
    )
    assert first["rtc"]["delay_steps"] == 0
    assert first["rtc"]["committed_actions"].shape == (0, 14)


def test_rtc_envelope_rejects_unknown_tick_bad_image_and_excess_delay():
    with pytest.raises(ValueError, match="target_start_tick"):
        build_rtc_request(observation(), target_start_tick=None, committed_actions=[], max_delay_steps=10)
    with pytest.raises(ValueError, match="delay range"):
        build_rtc_request(
            observation(), target_start_tick=1, committed_actions=np.zeros((11, 14)),
            max_delay_steps=10,
        )
    obs = observation()
    obs["observation.images.left_rgb"] = np.zeros((4, 6, 3), dtype=np.float32)
    with pytest.raises(ValueError, match="left image"):
        build_rtc_request(obs, target_start_tick=1, committed_actions=[], max_delay_steps=10)


@pytest.mark.parametrize("rtc_used,alter_prefix,valid_metadata", [
    (True, False, True),
    (False, False, True),
    (True, True, True),
    (True, False, False),
])
def test_rtc_wire_refuses_ordinary_fallback_and_changed_prefix(
    rtc_used, alter_prefix, valid_metadata
):
    pytest.importorskip("openpi_client")
    from openpi_client import msgpack_numpy
    from websockets.sync.server import serve

    received = []

    def handler(ws):
        packer = msgpack_numpy.Packer()
        meta = {"rtc_mode": "trained" if valid_metadata else "off",
                "rtc_max_delay_steps": 10, "action_horizon": 50, "action_dim": 14}
        ws.send(packer.pack(meta))
        if not valid_metadata:
            return
        request = msgpack_numpy.unpackb(ws.recv(timeout=2))
        received.append(request)
        actions = np.zeros((50, 14))
        prefix = request["rtc"]["committed_actions"]
        actions[:len(prefix)] = prefix
        if alter_prefix:
            actions[0, 0] += 0.01
        ws.send(packer.pack({"actions": actions, "server_timing": {"rtc_used": rtc_used}}))

    with serve(handler, "127.0.0.1", 0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"ws://127.0.0.1:{server.socket.getsockname()[1]}"
        prefix = np.zeros((2, 14))
        prefix[0, 0] = 0.1
        client = None
        try:
            if not valid_metadata:
                with pytest.raises(ValueError, match="trained RTC"):
                    RtcPolicyClient(url, timeout=2)
            else:
                client = RtcPolicyClient(url, timeout=2)
                if not rtc_used or alter_prefix:
                    with pytest.raises(ValueError, match="RTC|committed"):
                        client.infer_rtc(
                            observation(), target_start_tick=42, committed_actions=prefix
                        )
                else:
                    result = client.infer_rtc(
                        observation(), target_start_tick=42, committed_actions=prefix
                    )
                    assert np.asarray(result["actions"]).shape == (50, 14)
                    assert received[0]["rtc"]["target_start_tick"] == 42
                    assert received[0]["rtc"]["observation_policy_tick"] == 42
                    assert received[0]["rtc"]["delay_steps"] == 2
        finally:
            if client is not None:
                client.close()
            server.shutdown()
            thread.join(timeout=2)
