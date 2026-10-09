"""Soft gripper force wiring and the official stalled-contact behavior, without CAN."""

from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from yam_abc_reproduce.config import (
    RobotConfig,
    RobotUnitConfig,
    StationConfig,
    apply_station_form,
    build_station_config,
)
from yam_abc_reproduce.robot import yam_adapter


@pytest.mark.parametrize("bad", [None, True, "20", 0, -1, float("nan"), float("inf")])
def test_force_config_rejects_invalid_or_disabled_limits(bad):
    with pytest.raises(ValueError, match="gripper_force_limit_n"):
        RobotUnitConfig(gripper_force_limit_n=bad)


def test_force_units_default_yaml_and_station_form(tmp_path):
    assert RobotUnitConfig().gripper_force_limit_n == 50.0
    path = tmp_path / "station.yaml"
    path.write_text(yaml.safe_dump({"robot": {"robots": [
        {"type": "yam_left", "gripper_force_limit_n": 20},
        {"type": "yam_right", "gripper_force_limit_n": 30},
    ]}}))
    base = build_station_config(path)
    assert [r.gripper_force_limit_n for r in base.robot.robots] == [20, 30]
    cfg = apply_station_form(base, {"robots": [
        {"type": "yam_left", "gripper": "linear_4310"},
        {"type": "yam_right", "gripper": "flexible_4310"},
    ]})
    # Force is in N for either gripper geometry. A rail edit must not silently
    # raise the operator's limit back to 50 N when changing gripper type.
    assert [r.gripper_force_limit_n for r in cfg.robot.robots] == [20, 30]


def test_runtime_passes_independent_force_limits_to_common_follower_adapter(monkeypatch):
    from yam_abc_reproduce.runtime import build_arm_units

    built = []

    def fake_robot(**kwargs):
        built.append(kwargs)
        return SimpleNamespace(stop=lambda: None, gripper_limits=lambda: [6.9, 1.2])

    monkeypatch.setattr(yam_adapter, "YamRobot", fake_robot)
    cfg = StationConfig(robot=RobotConfig(robots=[
        RobotUnitConfig("yam_left", gripper_force_limit_n=20),
        RobotUnitConfig("yam_right", gripper_force_limit_n=30),
    ]))
    units = build_arm_units(cfg, followers_only=True)
    assert [u.name for u in units] == ["left", "right"]
    assert [b["gripper_force_limit_n"] for b in built] == [20, 30]


def test_adapter_forwards_before_startup_without_changing_arm_or_gripper_targets(monkeypatch):
    import i2rt.robots.get_robot as sdk

    built = []
    commands = []
    monkeypatch.setattr(sdk, "get_yam_robot", lambda **kw: built.append(kw) or SimpleNamespace(
        command_joint_pos=lambda q: commands.append(q.copy()),
    ))
    monkeypatch.setattr(yam_adapter, "construct_owned_yam", lambda channel, factory: factory())
    robot = yam_adapter.YamRobot("can_never_opened", gripper_force_limit_n=20,
                                gripper_limits=[6.9, 1.2])
    assert built[0]["limit_gripper_force"] == 20
    np.testing.assert_array_equal(built[0]["gripper_limits_override"], [6.9, 1.2])
    action = np.array([.1, .2, .3, .4, .5, .6, .15])
    robot.command_joint_pos(action)
    np.testing.assert_array_equal(commands[0], action)


def test_missing_sdk_support_refuses_before_can_or_default_force_fallback(monkeypatch):
    import i2rt.robots.get_robot as sdk

    calls = []
    monkeypatch.setattr(sdk, "get_yam_robot", lambda channel: calls.append(channel))
    monkeypatch.setattr(yam_adapter, "construct_owned_yam", lambda *a: calls.append(a))
    with pytest.raises(RuntimeError, match="configurable gripper force limit missing"):
        yam_adapter.YamRobot("can_never_opened", gripper_force_limit_n=20)
    assert calls == []


@pytest.mark.parametrize("bad", [None, True, "20", 0, -1, float("nan"), float("inf")])
def test_direct_adapter_invalid_force_does_not_claim_can(monkeypatch, bad):
    calls = []
    monkeypatch.setattr(yam_adapter, "construct_owned_yam", lambda *a: calls.append(a))
    with pytest.raises(ValueError, match="gripper_force_limit_n"):
        yam_adapter.YamRobot("can_never_opened", gripper_force_limit_n=bad)
    assert calls == []


def test_force_parameter_does_not_change_teaching_handle_factory(monkeypatch):
    import i2rt.robots.get_robot as sdk

    built = []
    monkeypatch.setattr(sdk, "get_yam_robot", lambda **kw: built.append(kw))
    monkeypatch.setattr(yam_adapter, "construct_owned_yam", lambda channel, factory: factory())
    yam_adapter._build_yam("can_never_opened", "yam", "yam_teaching_handle", None)
    assert "limit_gripper_force" not in built[0]


def test_patched_sdk_factory_reaches_motor_robot_with_pinned_range_and_original_pd(monkeypatch):
    import i2rt.robots.get_robot as sdk
    from i2rt.robots.utils import ArmType, GripperType

    built = []

    class Chain:
        def __init__(self, motors, offsets, *args, **kwargs):
            self.motor_offset = np.array(offsets)
            self.states = [SimpleNamespace(pos=0.) for _ in motors]
            self.states[-1].pos = 4.

        def read_states(self):
            return self.states

        def start_thread(self):
            pass

    monkeypatch.setattr(sdk, "DMChainCanInterface", Chain)
    monkeypatch.setattr(sdk, "MotorChainRobot", lambda **kw: built.append(kw) or kw)
    monkeypatch.setattr(sdk, "combine_arm_and_gripper_xml", lambda *a, **kw: "unused.xml")
    monkeypatch.setattr(sdk, "_load_joint_limits_from_xml", lambda *a: np.tile([-3., 3.], (7, 1)))
    sdk.get_yam_robot(arm_type=ArmType.YAM, gripper_type=GripperType.LINEAR_4310,
                      gripper_limits_override=np.array([6.9, 1.2]), limit_gripper_force=20)
    assert built[0]["limit_gripper_force"] == 20
    assert not built[0]["enable_gripper_calibration"]
    np.testing.assert_array_equal(built[0]["gripper_limits"], [6.9, 1.2])
    assert built[0]["kp"][-1] == 20.0 and built[0]["kd"][-1] == .5


def test_patched_sdk_force_validation_and_gripper_only_forwarding_before_can(monkeypatch):
    import i2rt.robots.get_robot as sdk
    from i2rt.robots.utils import ArmType

    built = []
    monkeypatch.setattr(sdk, "_get_gripper_only_robot", lambda **kw: built.append(kw))
    for bad in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="limit_gripper_force"):
            sdk.get_yam_robot(arm_type=ArmType.NO_ARM, limit_gripper_force=bad)
    assert built == []
    sdk.get_yam_robot(arm_type=ArmType.NO_ARM, limit_gripper_force=30)
    assert built[0]["limit_gripper_force"] == 30


@pytest.mark.parametrize("force_n", [10, 20, 50])
def test_official_limiter_force_conversion_and_opening_release(force_n):
    from i2rt.robots.utils import ArmType, GripperForceLimiter, GripperType

    limiter = GripperForceLimiter(force_n, GripperType.LINEAR_4310, ArmType.YAM, kp=20.)
    state = dict(target_qpos=6.9, current_qpos=4., current_qvel=0., current_eff=.8,
                 current_normalized_qpos=.5, target_normalized_qpos=.1, last_command_qpos=4.04)
    limited = limiter.update(state)
    # Official linear mapping plus the SDK's 0.3 Nm friction allowance. This
    # number is a steady contact target, NOT a bound on transient motor effort.
    assert limiter.compute_target_gripper_torque(state) == pytest.approx(force_n*.096/6.57 + .3)
    assert limited < state["target_qpos"]
    opening = {**state, "target_qpos": 1.2, "target_normalized_qpos": .9}
    assert limiter.update(opening) == 1.2


def test_official_limiter_does_not_claim_to_cap_fast_or_precontact_commands():
    from i2rt.robots.utils import ArmType, GripperForceLimiter, GripperType

    limiter = GripperForceLimiter(20, GripperType.LINEAR_4310, ArmType.YAM, kp=20.)
    state = dict(target_qpos=6.9, current_qpos=4., current_qvel=.4, current_eff=.8,
                 current_normalized_qpos=.5, target_normalized_qpos=.1, last_command_qpos=4.)
    assert limiter.update(state) == 6.9  # Not stalled: no false hard-limit guarantee.


def test_station_selects_ten_newtons_without_changing_calibration():
    from pathlib import Path

    cfg = build_station_config(Path(__file__).resolve().parents[1] / "configs/station_hil.yaml")
    left, right = cfg.robot.robots
    assert left.gripper_force_limit_n == right.gripper_force_limit_n == 10.0
    assert left.gripper_limits == [6.9, 1.2289234760051873]
    assert right.gripper_limits == [6.9, 1.2434195468070488]
