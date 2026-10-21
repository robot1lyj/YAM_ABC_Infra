"""Leader-only session tuning: no Follower reconnect, trajectory change or SDK reload."""
import json
import threading
import time
from types import SimpleNamespace as NS

import numpy as np
import pytest
import uvicorn
from fastapi.testclient import TestClient

from yam_abc_reproduce.config import StationConfig, build_station_config
from yam_abc_reproduce.hil.core import Phase
from yam_abc_reproduce.hil.device_client import DeviceClient
from yam_abc_reproduce.hil.recording import RecordingSession
from yam_abc_reproduce.hil.run import Runtime
from yam_abc_reproduce.hil.station import StationIO
from yam_abc_reproduce.hil.storage import read_rows
from yam_abc_reproduce.hil.web import create_app
from yam_abc_reproduce.robot.yam_adapter import YamLeaderArm, YamTeleop
from yam_abc_reproduce.runtime import build_arm_units


@pytest.fixture
def owner():
    cfg = build_station_config("configs/station_hil.yaml")
    io = StationIO(build_arm_units(StationConfig(), mock=True, followers_only=True), mock=True)
    recorder = NS(recording=False, saving=False)
    runtime = Runtime(io, [NS(role=r) for r in ("top", "left", "right")], None, recorder,
        mode="hil", leader_config=cfg.robot, leader_factory=lambda: iter(()))
    try:
        yield runtime, cfg
    finally:
        runtime.leader_connection.thread = None  # A test may have injected a busy placeholder.
        io.mock = True
        io.close()


def apply(owner):
    _, command, receipt = owner.policy_commands.get_nowait()
    q = np.full(14, .2)
    owner._apply_settings_command(command, q, q, receipt)
    return receipt


def test_adapter_swaps_one_complete_array_without_pd_or_position_commands():
    original = np.full(6, 1.1)
    commands = NS(kp=np.ones(6) * 80, kd=np.ones(6) * 5, pos=np.arange(6.))
    device = NS(gravity_comp_factor=original, _commands=commands, use_coulomb_friction=True)
    arm = YamLeaderArm.__new__(YamLeaderArm)
    arm._robot, arm._n, arm._hil_manual = device, 6, False
    teleop = YamTeleop(arm, NS(num_dofs=lambda: 7))
    teleop.hil_set_leader_gravity(1.05)
    np.testing.assert_array_equal(original, np.full(6, 1.1))
    np.testing.assert_array_equal(device.gravity_comp_factor, np.full(6, 1.05))
    assert device._commands is commands and device.use_coulomb_friction
    assert arm._hil_manual is False


@pytest.mark.parametrize("mode", ["teleop", "hil"])
def test_admission_only_queues_then_tick_updates_both_leaders_and_shared_factory_config(owner, mode):
    runtime, cfg = owner
    a = runtime.session.arbiter
    a.mode = mode
    a.hold(np.arange(14, dtype=float) / 100)
    before_hold, epoch, timeline = a._hold.copy(), a.epoch, a.rtc_timeline
    followers = [u.robot for u in runtime.io.units]
    objects = []
    for _ in followers:
        leader = YamLeaderArm.__new__(YamLeaderArm)
        leader._robot = NS(gravity_comp_factor=np.full(6, 1.1))
        leader._n = 6
        agent = NS(hil_set_leader_gravity=leader.set_gravity_comp_factor, close_hil=lambda: None)
        objects.append((leader, agent))
    runtime.io.set_leaders([agent for _, agent in objects])
    runtime.io.mock = False  # Exercise real adapter setters; no CAN objects exist.
    runtime.configure_leader_gravity(gravity_comp_factor=1.05, ready=True)
    assert cfg.robot.leader_gravity_comp_factor == 1.1
    assert runtime.policy_command_status["state"] == "queued"
    assert apply(runtime)["state"] == "accepted"
    assert cfg.robot.leader_gravity_comp_factor == 1.05
    for leader, _ in objects:
        np.testing.assert_array_equal(leader._robot.gravity_comp_factor, np.full(6, 1.05))
    assert [u.robot for u in runtime.io.units] == followers
    assert all(not f._stopped for f in followers)
    np.testing.assert_array_equal(a._hold, before_hold)
    assert (a.epoch, a.rtc_timeline) == (epoch, timeline)
    assert not runtime.leader_connection.busy


def test_update_while_leaders_disabled_is_used_by_next_factory_and_does_not_enable(owner, monkeypatch):
    from yam_abc_reproduce.robot import yam_adapter
    from yam_abc_reproduce.runtime import iter_leader_agents

    runtime, cfg = owner
    runtime.configure_leader_gravity(gravity_comp_factor=1.15, ready=True)
    assert apply(runtime)["state"] == "accepted"
    assert cfg.robot.leader_gravity_comp_factor == 1.15
    assert not runtime.io.leaders_connected and not runtime.leader_connection.busy
    assert runtime.leader_settings()["gravity_comp_factor"] == 1.15
    built = []
    monkeypatch.setattr(yam_adapter, 'YamLeaderArm', lambda **kw: built.append(kw) or NS())
    monkeypatch.setattr(yam_adapter, 'YamTeleop', lambda *a, **kw: NS())
    assert len(list(iter_leader_agents(cfg, runtime.io.units))) == 2
    assert [o['gravity_comp_factor'] for o in built] == [1.15, 1.15]
    # Neither the factory's session config nor tuning mutates the station file.
    assert build_station_config("configs/station_hil.yaml").robot.leader_gravity_comp_factor == 1.1


@pytest.mark.parametrize("change", ["phase", "recording", "saving", "maintenance", "intervention", "stop", "connecting"])
def test_admission_and_execution_recheck_hold_and_lifecycle(owner, change):
    runtime, cfg = owner
    runtime.configure_leader_gravity(gravity_comp_factor=1.05, ready=True)
    if change == "phase":
        runtime.session.arbiter.phase = Phase.HUMAN
    elif change in ("recording", "saving"):
        setattr(runtime.recorder, change, True)
    elif change == "maintenance":
        runtime.maintenance.state = "gravity"
    elif change == "intervention":
        runtime.session.arbiter.intervention_pending = True
    elif change == "stop":
        runtime.emergency.set()
    else:
        runtime.leader_connection.thread = NS(is_alive=lambda: True)
    assert not runtime.leader_settings()["allowed"]
    with pytest.raises(ValueError):
        runtime.configure_leader_gravity(gravity_comp_factor=1.05, ready=True)
    assert apply(runtime)["state"] == "rejected"
    assert cfg.robot.leader_gravity_comp_factor == 1.1


@pytest.mark.parametrize("value", [None, True, "1.1", 0, .4, 1.6, float("nan"), float("inf")])
def test_invalid_settings_never_enter_command_queue(owner, value):
    runtime, _ = owner
    with pytest.raises(ValueError):
        runtime.configure_leader_gravity(gravity_comp_factor=value, ready=True)
    assert runtime.policy_commands.empty()


def test_confirmation_and_http_schema_are_enforced(owner):
    runtime, _ = owner
    headers = {"X-YAM-Control": "1"}
    with TestClient(create_app(runtime)) as client:
        assert client.post('/leaders/settings', headers=headers,
            json={'gravity_comp_factor': 1.05}).status_code == 409
        for body in ({'gravity_comp_factor': True, 'ready': True},
                     {'gravity_comp_factor': '1.1', 'ready': True},
                     {'gravity_comp_factor': 1.6, 'ready': True},
                     {'gravity_comp_factor': 1.1, 'ready': True, 'follower_factor': 1.1}):
            assert client.post('/leaders/settings', headers=headers, json=body).status_code == 422
        assert runtime.policy_commands.empty()
        assert client.post('/leaders/settings', headers=headers,
            json={'gravity_comp_factor': 1.05, 'ready': True}).status_code == 200
    assert runtime.policy_command_status['state'] == 'queued'


def test_web_unix_proxy_forwards_to_persistent_owner_without_restart(tmp_path, owner):
    runtime, _ = owner
    socket = tmp_path / 'leader.sock'
    server = uvicorn.Server(uvicorn.Config(create_app(runtime), uds=str(socket), log_level='critical'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 3
    while not server.started and time.monotonic() < deadline:
        time.sleep(.01)
    assert server.started
    try:
        proxy = DeviceClient(socket)
        proxy.configure_leader_gravity(gravity_comp_factor=1.05, ready=True)
        assert runtime.policy_command_status['state'] == 'queued'
        assert apply(runtime)['state'] == 'accepted'
        assert thread.is_alive() and runtime.session.arbiter.phase == Phase.HOLD
    finally:
        server.should_exit = True
        thread.join(3)


def test_recording_contains_selected_and_actual_parameters(tmp_path):
    cfg = build_station_config('configs/station_hil.yaml')
    io = StationIO(build_arm_units(cfg, mock=True, followers_only=True), mock=True)
    recorder = RecordingSession(tmp_path / 'session', mode='teleop', defer_storage=True)
    runtime = Runtime(io, [NS(role=r, history=lambda: []) for r in ('top', 'left', 'right')],
        None, recorder, mode='teleop', leader_config=cfg.robot)
    actual = [{'name': 'left', 'gravity_comp_factor': [1.05] * 6}]
    io.leader_control_status = lambda: actual
    rows = []
    submit = recorder.submit
    recorder.submit = lambda row, images: rows.append(dict(row)) or submit(row, images)
    try:
        runtime.configure_leader_gravity(gravity_comp_factor=1.05, ready=True)
        assert apply(runtime)['state'] == 'accepted'
        result = runtime.run(duration=.12)
        assert result['tick'] >= 1 and not result.get('error')
        assert rows[0]['leader_control'] == actual
        assert rows[0]['leader_gravity_comp_factor'] == 1.05
        assert recorder.metadata['leader_gravity_comp_factor'] == 1.05
        recorder.start_episode()
        for row in list(rows):
            assert submit(row, {})
        recorder.stop_episode()
        recorder.close()
        assert recorder.error is None
        episode = tmp_path / 'session/episode_000001'
        saved = list(read_rows(episode))
        assert saved[0]['leader_control'] == actual
        assert saved[0]['leader_gravity_comp_factor'] == 1.05
        assert json.loads((episode / 'manifest.json').read_text())['leader_gravity_comp_factor'] == 1.05
    finally:
        recorder.close()
        io.close()
