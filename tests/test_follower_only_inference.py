"""Ordinary inference owns Followers only; no hidden Leader SDK or CAN owner."""
from types import SimpleNamespace as NS

import numpy as np
import pytest

from yam_abc_reproduce.config import build_station_config
from yam_abc_reproduce.hil.core import Arbiter, Mode
from yam_abc_reproduce.hil.device_health import DeviceHealth
from yam_abc_reproduce.hil.run import Runtime, prepare_station_can, station_can_channels
from yam_abc_reproduce.hil.station import StationIO
from yam_abc_reproduce.runtime import ArmUnit, build_arm_units


class Follower:
    def __init__(self, **kwargs):
        self.commands = []
        self.closed = False

    def get_joint_pos(self):
        return np.zeros(7)

    def joint_limits(self):
        return np.tile([-3., 3.], (6, 1))

    def gripper_limits(self):
        return [6.9, 1.2]

    def hil_read(self):
        return self.get_joint_pos(), .01

    def hil_health(self):
        return dict(running=True, feedback_age_s=.01, motors=[
            dict(joint=f"J{i+1}", error_code=1) for i in range(6)])

    def command_joint_pos(self, value):
        self.commands.append(value.copy())

    def close_hil(self):
        self.closed = True


def test_factory_never_constructs_leader_or_requires_its_controller(monkeypatch):
    from yam_abc_reproduce.robot import yam_adapter
    cfg = build_station_config("configs/station_hil.yaml")
    cfg.robot.controllers = []
    monkeypatch.setattr(yam_adapter, "YamRobot", Follower)
    monkeypatch.setattr(yam_adapter, "YamLeaderArm",
                        lambda **kw: pytest.fail("Leader must never be energized"))
    units = build_arm_units(cfg, followers_only=True)
    assert len(units) == 2 and all(u.agent is None for u in units)


def test_can_prepare_and_shutdown_scope_excludes_leaders(monkeypatch):
    from yam_abc_reproduce.hil import run
    cfg = build_station_config("configs/station_hil.yaml")
    checked, brought_up = [], []
    def check(channels):
        checked.append(channels)
        return channels if len(checked) == 1 else []
    monkeypatch.setattr(run, "check_can_up", check)
    monkeypatch.setattr(run, "bring_up_can_buses", lambda channels: brought_up.append(channels) or [])
    assert station_can_channels(cfg, followers_only=True) == ["can_left", "can_right"]
    assert "can_lead" not in prepare_station_can(cfg, followers_only=True)
    assert checked == [["can_left", "can_right"]] * 2
    assert brought_up == [["can_left", "can_right"]]


def test_read_apply_hold_close_and_health_touch_only_followers():
    units = [ArmUnit(name, Follower(), None) for name in ("left", "right")]
    io = StationIO(units, mock=False)
    try:
        q, leader, buttons, ages = io.read()
        assert io._read_pool._max_workers == 4  # Capacity for later independent Leader activation.
        assert ages == [.01, None, .01, None]
        assert buttons == [[False, False], [False, False]]
        assert io.leader_control_status() == []
        assert [a["label"] for a in io.device_health()] == ["左 Follower", "右 Follower"]
        arbiter = Arbiter(Mode.INFERENCE)
        decision = arbiter.step(q, leader, now=0, dt=1/30, leader_ready=True)
        submitted, stamps = io.apply(decision, q, leader, dt=1/30, mirror=False)
        np.testing.assert_array_equal(submitted, q)
        assert set(stamps) == {"left_follower", "right_follower"}
        assert not io.leader_position_controlled and not io._manual
        assert io.hold() == []
        assert all(len(u.robot.commands) == 2 for u in units)
    finally:
        assert io.close() == []
    assert all(u.robot.closed for u in units)


def test_only_follower_health_is_required_but_missing_follower_still_blocks():
    health = DeviceHealth(expected_arms=2)
    reports = [dict(label=name, **Follower().hil_health()) for name in ("左 Follower", "右 Follower")]
    health.update(reports, now=1, follower=np.zeros(14), leader=np.zeros(14))
    assert health.snapshot(now=1)["ok"]
    health.update(reports[:1], now=2, follower=np.zeros(14), leader=np.zeros(14))
    assert not health.snapshot(now=2)["ok"]


def test_follower_home_ignores_saved_leader_pose_and_cannot_capture_fake_feedback():
    from yam_abc_reproduce.hil.maintenance import Maintenance
    maintenance = Maintenance(leaders_connected=False)
    q, leader = np.zeros(14), np.zeros(14)
    target = q.copy()
    target[0] = .1
    saved_leader = leader.copy()
    saved_leader[0] = 2.
    maintenance.load(dict(follower=target, leader=saved_leader))
    maintenance.command("home", q, leader, now=0, paused=True)
    assert maintenance.home_duration == pytest.approx(.1 / maintenance.HOME_SPEED)
    assert maintenance.home_start[1] is None
    _, leader_command = maintenance.step(q, leader, now=.03, dt=1/30)
    assert leader_command is None
    with pytest.raises(ValueError, match="完整四臂准备位"):
        maintenance.capture(q, leader)


@pytest.mark.parametrize("followers_only,mode", [(True, "inference"), (False, "hil")])
def test_mode_change_cannot_implicitly_enable_or_release_leaders(followers_only, mode):
    cfg = build_station_config("configs/station_hil.yaml")
    io = StationIO(build_arm_units(cfg, mock=True, followers_only=followers_only), mock=True)
    runtime = Runtime(io, [NS(role=r) for r in ("top", "left", "right")], None,
                      NS(recording=False, saving=False), mode=mode)
    try:
        runtime.worker = NS(ready=True)
        runtime.event("mode:hil" if followers_only else "mode:inference")
        event, _ = runtime.events.get_nowait()
        q, leader, _, _ = io.read()
        runtime.session.tick(q, leader, now=0, dt=1/30, observation_id=0, event=event)
        assert runtime.session.arbiter.mode == ("hil" if followers_only else "inference")
        assert io.leaders_connected is not followers_only
        if followers_only:
            with pytest.raises(ValueError, match="尚未启用"):
                runtime.event("home_leader")
    finally:
        io.close()
