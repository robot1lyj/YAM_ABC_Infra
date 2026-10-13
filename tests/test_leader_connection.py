"""Independent Leader lifetime: no Follower close, blocked tick, or implicit resume."""
import threading
import time
from types import SimpleNamespace as NS

import numpy as np
import pytest

from yam_abc_reproduce.config import StationConfig
from yam_abc_reproduce.hil.leader_connection import LeaderConnection
from yam_abc_reproduce.hil.recording import RecordingSession
from yam_abc_reproduce.hil.run import Runtime
from yam_abc_reproduce.hil.station import StationIO
from yam_abc_reproduce.runtime import build_arm_units, iter_leader_agents


class Agent:
    def __init__(self, fail_close=False):
        self.closed = 0
        self.fail_close = fail_close
        self.commands = []

    def close_hil(self):
        self.closed += 1
        if self.fail_close:
            self.fail_close = False
            raise RuntimeError("injected close failure")

    def hil_read(self):
        return np.full(7, .3), [False, False], .01

    def hil_leader_command(self, target, **settings):
        self.commands.append((target.copy(), settings))


def io_only():
    return StationIO(build_arm_units(StationConfig(), mock=True, followers_only=True), mock=True)


def finish(connection, **options):
    for _ in range(3):
        if not connection.busy:
            break
        connection.thread.join(2)
        assert not connection.thread.is_alive()
        connection.poll(**options)
    assert not connection.busy


def test_attach_detach_and_reactivate_preserve_follower_objects_and_power():
    io = io_only()
    original = [u.robot for u in io.units]
    prepared, stopped = [], []
    connection = LeaderConnection(io, lambda: iter_leader_agents(StationConfig(), io.units, mock=True),
        prepare=lambda: prepared.append(True), stop=lambda: stopped.append(True))
    try:
        for _ in range(2):
            connection.start(True)
            finish(connection)
            assert io.leaders_connected
            connection.start(False)
            finish(connection)
            assert not io.leaders_connected
            assert [u.robot for u in io.units] == original
            assert all(not robot._stopped for robot in original)
        assert len(prepared) == len(stopped) == 2
    finally:
        connection.close()
        io.close()


def test_partial_connect_failure_closes_only_new_leader_and_keeps_followers():
    io, first = io_only(), Agent()
    stopped = []
    def factory():
        yield first
        raise RuntimeError("right Leader did not answer")
    connection = LeaderConnection(io, factory, stop=lambda: stopped.append(True))
    try:
        connection.start(True)
        finish(connection)
        assert connection.state == "fault" and "right Leader" in connection.error
        assert first.closed == 1 and stopped == [True]
        assert not io.leaders_connected
        assert all(not u.robot._stopped for u in io.units)
    finally:
        connection.close()
        io.close()


def test_close_failure_retains_owner_and_retry_does_not_reclose_successes():
    io, left, right = io_only(), Agent(fail_close=True), Agent()
    stopped = []
    connection = LeaderConnection(io, lambda: iter([left, right]), stop=lambda: stopped.append(True))
    try:
        connection.start(True)
        finish(connection)
        connection.start(False)
        finish(connection)
        assert connection.error and not io.leaders_connected
        assert connection._owners == [left] and not stopped
        assert left.closed == right.closed == 1
        connection.start(False)
        finish(connection)
        assert connection.state == "disconnected" and connection.error is None
        assert left.closed == 2 and right.closed == 1 and stopped == [True]
        assert all(not u.robot._stopped for u in io.units)
    finally:
        connection.close()
        io.close()


@pytest.mark.parametrize("cancel_before_done", [False, True])
def test_cancelled_enable_never_attaches_late_leaders(cancel_before_done):
    io, released, entered = io_only(), threading.Event(), threading.Event()
    agents = [Agent(), Agent()]
    def factory():
        entered.set()
        released.wait(2)
        yield from agents
    connection = LeaderConnection(io, factory)
    try:
        connection.start(True)
        assert entered.wait(1)
        if cancel_before_done:
            connection.poll(cancelled=True)
        released.set()
        finish(connection, cancelled=True)
        assert not io.leaders_connected
        assert not connection._owners
        assert agents[0].closed == 1
        assert all(not u.robot._stopped for u in io.units)
    finally:
        released.set()
        connection.close()
        io.close()


def test_real_activation_holds_own_pose_with_native_gain_not_follower_target():
    io, agents = io_only(), [Agent(), Agent()]
    io.mock = False  # Exercise bootstrap commands without any hardware.
    connection = LeaderConnection(io, lambda: iter(agents))
    try:
        connection.start(True)
        finish(connection)
        for agent in agents:
            target, settings = agent.commands[0]
            np.testing.assert_array_equal(target, np.full(6, .3))
            assert settings == {"manual": False, "gain_scale": 1.}
    finally:
        connection.close()
        io.mock = True
        io.close()


def test_slow_constructor_does_not_block_follower_control_ticks(tmp_path):
    io, entered, release = io_only(), threading.Event(), threading.Event()
    def factory():
        entered.set()
        release.wait(3)
        yield from (Agent(), Agent())
    recorder = RecordingSession(tmp_path / "idle", mode="teleop", defer_storage=True)
    runtime = Runtime(io, [NS(role=r, history=lambda: []) for r in ("top", "left", "right")],
        None, recorder, mode="teleop", leader_factory=factory)
    try:
        runtime.configure_leaders(enabled=True, ready=True)
        _, command, receipt = runtime.policy_commands.get_nowait()
        q, leader, _, _ = io.read()
        runtime._apply_settings_command(command, q, leader, receipt)
        assert entered.wait(1)
        with pytest.raises(ValueError, match="独立启停"):
            runtime.event("start")
        status = runtime.run(duration=.18)
        assert status["tick"] >= 3 and not status.get("error")
        assert status["phase"] == "hold" and runtime.leader_connection.busy
        assert all(not u.robot._stopped for u in io.units)
    finally:
        release.set()
        recorder.close()
        io.close()


def test_leader_start_and_confirmation_gates_do_not_prevent_mode_selection(tmp_path):
    io = io_only()
    runtime = Runtime(io, [NS(role=r) for r in ("top", "left", "right")], None,
        NS(recording=False, saving=False), mode="teleop", leader_factory=lambda: iter([Agent(), Agent()]))
    try:
        with pytest.raises(ValueError, match="启用 Leader"):
            runtime.event("start")
        with pytest.raises(ValueError, match="确认Leader"):
            runtime.configure_leaders(enabled=True)
        runtime.configure_leaders(enabled=True, ready=True)
        _, command, receipt = runtime.policy_commands.get_nowait()
        runtime.session.arbiter.phase = "policy"
        runtime._apply_settings_command(command, np.zeros(14), np.zeros(14), receipt)
        assert receipt["state"] == "rejected" and not runtime.leader_connection.busy
        assert all(not u.robot._stopped for u in io.units)
    finally:
        io.close()


def test_queued_teleop_start_is_rechecked_after_leaders_were_disabled(tmp_path):
    io = io_only()
    io.set_leaders(list(iter_leader_agents(StationConfig(), io.units, mock=True)))
    recorder = RecordingSession(tmp_path / "idle", mode="teleop", defer_storage=True)
    runtime = Runtime(io, [NS(role=r, history=lambda: []) for r in ("top", "left", "right")],
        None, recorder, mode="teleop", leader_factory=lambda: iter(()))
    try:
        runtime.device_health.update(io.device_health(), now=time.monotonic(),
                                     follower=np.zeros(14), leader=np.zeros(14))
        runtime.event("start")
        runtime.leader_connection.start(False)
        finish(runtime.leader_connection)
        status = runtime.run(duration=.12)
        assert status["phase"] == "hold" and not status["leaders_connected"]
        assert "启用 Leader" in status["operator_error"]
        assert all(not u.robot._stopped for u in io.units)
    finally:
        recorder.close()
        io.close()


def test_fast_enable_is_not_adopted_mid_tick_with_absent_leader_feedback():
    io = io_only()
    runtime = Runtime(io, [NS(role=r) for r in ("top", "left", "right")], None,
        NS(recording=False, saving=False), mode="teleop", leader_factory=lambda: iter([Agent(), Agent()]))
    original_start = runtime.leader_connection.start
    def instant_start(enabled):
        original_start(enabled)
        runtime.leader_connection.thread.join(2)
        assert not runtime.leader_connection.thread.is_alive()
    runtime.leader_connection.start = instant_start
    try:
        runtime.configure_leaders(enabled=True, ready=True)
        _, command, receipt = runtime.policy_commands.get_nowait()
        q, placeholder, _, _ = io.read()
        runtime._apply_settings_command(command, q, placeholder, receipt)
        assert receipt["state"] == "accepted"
        assert not io.leaders_connected and not runtime.leaders_connected
        # Adoption happens at next tick, immediately before its fresh read.
        runtime._refresh_leaders()
        assert io.leaders_connected and runtime.leaders_connected
        assert all(not u.robot._stopped for u in io.units)
    finally:
        io.close()


def test_disabled_leader_fault_no_longer_blocks_followers_but_relatches_on_enable():
    from yam_abc_reproduce.hil.device_health import DeviceHealth
    io = io_only()
    health = DeviceHealth(expected_arms=2)
    health.tracking_fault = dict(arm="右 Leader", joint="J4", message="Leader tracking fault")
    try:
        health.update(io.device_health(), now=1, follower=np.zeros(14), leader=np.zeros(14))
        assert health.snapshot(now=1)["ok"]
        assert health.snapshot(now=1)["tracking_fault"] is None
        health.expected_arms = 4
        assert health.blocking_reason(now=1) == "Leader tracking fault"
    finally:
        io.close()


def test_web_and_unix_client_expose_the_same_independent_operation():
    from fastapi.testclient import TestClient

    from yam_abc_reproduce.hil.device_client import DeviceClient
    from yam_abc_reproduce.hil.web import create_app
    calls = []
    owner = NS(configure_leaders=lambda **body: calls.append(body), status={})
    with TestClient(create_app(owner)) as client:
        result = client.post("/leaders/connection", json={"enabled": True, "ready": True},
                             headers={"X-YAM-Control": "1"})
    assert result.status_code == 200 and calls == [dict(enabled=True, ready=True)]
    proxy = DeviceClient.__new__(DeviceClient)
    proxy._request = lambda *args: args
    assert proxy.configure_leaders(enabled=False, ready=True) == (
        "POST", "/leaders/connection", dict(enabled=False, ready=True))
