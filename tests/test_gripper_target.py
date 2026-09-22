"""Absolute gripper UI commands share the existing single-owner jog safety path."""

from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from yam_abc_reproduce.hil.device_client import DeviceClient
from yam_abc_reproduce.hil.jog import Jog
from yam_abc_reproduce.hil.web import create_app


@pytest.mark.parametrize("arm,index", [("left", 6), ("right", 13)])
@pytest.mark.parametrize("target", [0.0, 0.15, 1.0])
def test_absolute_target_bounded_rate_and_other_joints_unchanged(arm, index, target):
    jog = Jog()
    q = np.linspace(-0.2, 0.2, 14)
    q[[6, 13]] = 0.8 if target < 0.8 else 0.1
    initial = q.copy()
    jog.request(arm, 6, target=target)
    steps = 0
    for tick in range(180):
        result = jog.step(q, allowed=True, now=tick / 30, dt=1 / 30)
        if result is None:
            break
        assert abs(result[index] - q[index]) <= 0.25 / 30 + 1e-12
        other = [j for j in range(14) if j != index]
        np.testing.assert_array_equal(result[other], initial[other])
        q = result
        steps += 1
    assert steps > 1
    assert q[index] == pytest.approx(target, abs=0.001)
    assert jog.target is None


@pytest.mark.parametrize("kwargs", [
    {}, {"delta": .1, "target": .15}, {"target": -0.1},
    {"target": 1.01}, {"target": float("nan")}, {"target": float("inf")},
])
def test_reject_invalid_absolute_requests(kwargs):
    with pytest.raises(ValueError):
        Jog().request("left", 6, **kwargs)


def test_absolute_is_gripper_only_and_pause_cancels_without_resuming():
    jog = Jog()
    with pytest.raises(ValueError):
        jog.request("left", 0, target=.15)
    jog.request("right", 6, target=.15)
    q = np.ones(14)
    assert jog.step(q, allowed=False, now=0, dt=1/30) is None
    assert jog.step(q, allowed=True, now=1, dt=1/30) is None
    jog.request("left", 6, target=.15)
    assert jog.step(q, allowed=True, now=2, dt=1/30) is not None
    assert jog.step(q, allowed=False, now=2.1, dt=1/30) is None
    assert jog.step(q, allowed=True, now=3, dt=1/30) is None


def test_absolute_target_expires_if_feedback_does_not_move():
    jog = Jog()
    jog.request("left", 6, target=.15)
    q = np.ones(14)
    assert jog.step(q, allowed=True, now=0, dt=1/30) is not None
    assert jog.step(q, allowed=True, now=6, dt=1/30) is None


def test_http_target_forwarding_and_bounds():
    calls = []
    jog = Jog()
    def request_jog(**kw):
        jog.request(**kw)
        calls.append(kw)
    runtime = SimpleNamespace(request_jog=request_jog)
    with TestClient(create_app(runtime)) as client:
        headers = {"X-YAM-Control": "1"}
        response = client.post("/jog", json={"arm": "right", "joint": 6, "target": .15}, headers=headers)
        assert response.status_code == 200
        assert calls == [{"arm": "right", "joint": 6, "target": .15}]
        for body in ({"target": 1.1}, {"target": -.1}, {"target": .15, "delta": -.1}):
            response = client.post("/jog", json={"arm": "left", "joint": 6, **body}, headers=headers)
            assert response.status_code in (409, 422)


def test_device_proxy_preserves_absolute_value(monkeypatch):
    client = DeviceClient("/not-opened.sock")
    calls = []
    monkeypatch.setattr(client, "_request", lambda *args: calls.append(args))
    client.request_jog("left", 6, target=.15)
    assert calls == [("POST", "/jog", {"arm": "left", "joint": 6, "target": .15, "delta": None})]
