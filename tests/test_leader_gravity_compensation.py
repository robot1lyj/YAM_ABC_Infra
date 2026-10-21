"""Leader-only gravity tuning uses the same factory for teleop and HIL, without CAN."""
from types import SimpleNamespace

import numpy as np
import pytest

from yam_abc_reproduce.config import RobotConfig, apply_station_form, build_station_config
from yam_abc_reproduce.robot import yam_adapter
from yam_abc_reproduce.runtime import build_arm_units, iter_leader_agents


def test_station_selects_uniform_leader_factor_without_changing_grippers():
    cfg = build_station_config("configs/station_hil.yaml")
    assert cfg.robot.leader_gravity_comp_factor == 1.1
    assert [r.gripper_force_limit_n for r in cfg.robot.robots] == [1., 1.]
    assert [r.gripper_limits[0] for r in cfg.robot.robots] == [6.9, 6.9]
    assert apply_station_form(cfg, {}).robot.leader_gravity_comp_factor == 1.1
    assert RobotConfig().leader_gravity_comp_factor is None


@pytest.mark.parametrize("bad", [True, "1.1", 0., -1., float("inf"), float("nan")])
def test_invalid_config_is_rejected_before_hardware_construction(bad):
    with pytest.raises(ValueError, match="leader_gravity_comp_factor"):
        RobotConfig(leader_gravity_comp_factor=bad)


def test_full_and_independent_leader_activation_share_the_override(monkeypatch):
    cfg = build_station_config("configs/station_hil.yaml")
    followers, leaders = [], []

    def follower(**options):
        followers.append(options)
        return SimpleNamespace(stop=lambda: None, gripper_limits=lambda: [6.9, 1.2])

    def leader(**options):
        leaders.append(options)
        return SimpleNamespace(close_hil=lambda: None)

    monkeypatch.setattr(yam_adapter, "YamRobot", follower)
    monkeypatch.setattr(yam_adapter, "YamLeaderArm", leader)
    monkeypatch.setattr(yam_adapter, "YamTeleop", lambda *a, **kw: SimpleNamespace())
    units = build_arm_units(cfg)
    assert [o["gravity_comp_factor"] for o in leaders] == [1.1, 1.1]
    assert all("gravity_comp_factor" not in o for o in followers)

    follower_objects = [u.robot for u in units]
    followers.clear()
    leaders.clear()
    assert len(list(iter_leader_agents(cfg, units))) == 2
    assert not followers  # Independent activation neither rebuilds nor disables Followers.
    assert [u.robot for u in units] == follower_objects
    assert [o["gravity_comp_factor"] for o in leaders] == [1.1, 1.1]


@pytest.mark.parametrize("factor", [None, 1.1])
def test_sdk_receives_replacement_factors_before_startup_and_retains_them_across_modes(
    monkeypatch, factor,
):
    import i2rt.robots.get_robot as sdk

    built = []
    native = np.array([1., 1.1, 1.1, 1.2, 1., 1.])

    def factory(**options):
        built.append(options)
        device = SimpleNamespace(
            _kp=np.ones(6) * 80, _kd=np.ones(6) * 5,
            gravity_comp_factor=options.get("gravity_comp_factor", native).copy(),
            use_coulomb_friction=options["use_coulomb_friction"],
            close=lambda: None, get_joint_pos=lambda: np.full(6, .2),
            command_joint_pos=lambda q: None,
        )
        def gains(kp, kd):
            device._commands = SimpleNamespace(kp=kp.copy(), kd=kd.copy())
        device.update_kp_kd = gains
        return device

    monkeypatch.setattr(sdk, "get_yam_robot", factory)
    monkeypatch.setattr(yam_adapter, "construct_owned_yam", lambda channel, factory: factory())
    leader = yam_adapter.YamLeaderArm("can_never_opened", gravity_comp_factor=factor)
    expected = native if factor is None else np.full(6, 1.1)
    if factor is None:
        assert "gravity_comp_factor" not in built[0]
    else:
        np.testing.assert_array_equal(built[0]["gravity_comp_factor"], expected)
    assert built[0]["use_coulomb_friction"] is True
    assert "limit_gripper_force" not in built[0]
    for manual in (False, True, False):
        leader.set_manual_control(manual)
        status = leader.control_status()
        np.testing.assert_array_equal(status["gravity_comp_factor"], expected)
        if manual:
            assert status["kp"] == status["kd"] == [0.] * 6
        assert status["friction_compensation"] is True
