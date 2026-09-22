import subprocess
import sys
import types
from types import SimpleNamespace

import numpy as np
import pytest

from yam_abc_reproduce.robot import can_bus, ownership, yam_adapter


@pytest.fixture
def lock_directory(tmp_path, monkeypatch):
    directory = tmp_path / "locks"
    monkeypatch.setattr(ownership, "LOCK_DIRECTORY", directory)
    return directory


def fake_sdk(*, close=None, gains=None):
    return SimpleNamespace(
        _kp=np.ones(6), _kd=np.ones(6),
        close=close or (lambda: None), update_kp_kd=gains or (lambda **kwargs: None),
    )


def fake_i2rt(monkeypatch, factory):
    sdk = types.ModuleType("i2rt")
    robots = types.ModuleType("i2rt.robots")
    get_robot = types.ModuleType("i2rt.robots.get_robot")
    utils = types.ModuleType("i2rt.robots.utils")
    sdk.robots, robots.get_robot = robots, get_robot
    get_robot._apply_arm_motor_wrap_offsets = lambda: None
    get_robot._align_gripper_limits_to_motor_position = lambda: None
    get_robot.get_yam_robot = factory

    class Enum:
        @staticmethod
        def from_string_name(value):
            if value == "invalid":
                raise ValueError("invalid SDK config")
            return value

    utils.ArmType = utils.GripperType = Enum
    for module in (sdk, robots, get_robot, utils):
        monkeypatch.setitem(sys.modules, module.__name__, module)


@pytest.mark.parametrize("adapter", [yam_adapter.YamRobot, yam_adapter.YamLeaderArm])
def test_both_adapters_exclude_other_owners_until_sdk_closes(lock_directory, monkeypatch, adapter):
    closed = []

    def close():
        with pytest.raises(ownership.CanOwnershipError):
            ownership.CanOwnership("can_test")
        closed.append(True)

    fake_i2rt(monkeypatch, lambda **kwargs: fake_sdk(close=close))
    arm = adapter("can_test")
    with pytest.raises(ownership.CanOwnershipError, match="can_test.*PID"):
        adapter("can_test")
    arm.close_hil()
    arm.close_hil()  # Idempotent teardown never touches a later owner's SDK.
    assert closed == [True]
    with ownership.CanOwnership("can_test"):
        pass


def test_safe_configuration_failure_and_completed_cleanup_allow_retry(lock_directory, monkeypatch):
    calls = []
    fake_i2rt(monkeypatch, lambda **kwargs: calls.append(kwargs) or fake_sdk())
    with pytest.raises(ValueError, match="invalid SDK config"):
        yam_adapter.YamRobot("can_test", arm_type="invalid")
    assert calls == []
    with ownership.CanOwnership("can_test"):
        pass

    closed = []

    def fail_gains(**kwargs):
        raise ValueError("injected gain setup failure")

    fake_i2rt(monkeypatch, lambda **kwargs: fake_sdk(close=lambda: closed.append(True), gains=fail_gains))
    with pytest.raises(ValueError, match="gain setup"):
        yam_adapter.YamLeaderArm("can_test")
    assert closed == [True]
    with ownership.CanOwnership("can_test"):
        pass


def test_failed_close_keeps_ownership_until_successful_retry(lock_directory):
    attempts = []

    def close():
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("SDK thread still alive")

    robot = ownership.construct_owned_yam("can_test", lambda: fake_sdk(close=close))
    with pytest.raises(RuntimeError, match="still alive"):
        robot.close()
    with pytest.raises(ownership.CanOwnershipError):
        ownership.CanOwnership("can_test")
    robot.close()
    with ownership.CanOwnership("can_test"):
        pass


@pytest.mark.parametrize("uncertain_startup", [False, True])
def test_cross_process_lock_and_uncertain_factory_release_only_on_exit(lock_directory, uncertain_startup):
    script = """
import sys
from pathlib import Path
from yam_abc_reproduce.robot import ownership
ownership.LOCK_DIRECTORY = Path(sys.argv[1])
if sys.argv[2] == 'True':
    def fail():
        raise RuntimeError('factory may have started a thread')
    try:
        ownership.construct_owned_yam('can_test', fail)
    except RuntimeError as exc:
        print(str(exc), flush=True)
else:
    lease = ownership.CanOwnership('can_test')
    print('locked', flush=True)
sys.stdin.readline()
"""
    process = subprocess.Popen([sys.executable, "-u", "-c", script, str(lock_directory), str(uncertain_startup)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        message = process.stdout.readline()
        assert "Ownership retained" in message if uncertain_startup else "locked" in message
        with pytest.raises(ownership.CanOwnershipError, match=f"PID {process.pid}"):
            ownership.CanOwnership("can_test")
        process.communicate("\n", timeout=5)
        assert process.returncode == 0
        # No stale-file deletion or PID guessing is required after process exit.
        assert (lock_directory / "can_test.lock").exists()
        with ownership.CanOwnership("can_test"):
            pass
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)


def test_reset_reserves_all_channels_before_any_mutation(lock_directory, monkeypatch):
    calls = []
    monkeypatch.setattr(can_bus, "list_can_interfaces", lambda: ["can_a", "can_b"])
    monkeypatch.setattr(can_bus.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    with ownership.CanOwnership("can_b"):
        ok, message = can_bus.reset_can_buses()
        assert not ok and "can_b" in message
    assert calls == []
    with ownership.CanOwnership("can_a"):
        pass  # Partial acquisition was released when reserving the second bus failed.


@pytest.mark.parametrize("operation", [can_bus.bring_up_can_buses, can_bus.stop_can_buses])
def test_can_change_refuses_current_owner_including_post_close_race(lock_directory, monkeypatch, operation):
    calls = []
    monkeypatch.setattr(can_bus.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    old_sdk = ownership.construct_owned_yam("can_test", fake_sdk)
    old_sdk.close()
    # Another entrypoint acquires the bus before the old entrypoint's CAN cleanup.
    with ownership.CanOwnership("can_test"):
        errors = operation(["can_test"])
        assert len(errors) == 1 and "owned" in errors[0]
    assert calls == []


def test_can_reset_holds_exact_enumerated_locks_during_commands(lock_directory, monkeypatch):
    calls = []
    monkeypatch.setattr(can_bus, "list_can_interfaces", lambda: ["can_a", "can_b"])

    def command(argv, **kwargs):
        calls.append(argv)
        for channel in ("can_a", "can_b"):
            with pytest.raises(ownership.CanOwnershipError):
                ownership.CanOwnership(channel)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(can_bus.subprocess, "run", command)
    assert can_bus.reset_can_buses()[0]
    assert [(args[5], args[6]) for args in calls] == [
        ("can_a", "down"), ("can_a", "up"), ("can_b", "down"), ("can_b", "up")]


def test_legacy_teardown_closes_retained_motorized_leader_and_follower():
    from yam_abc_reproduce.gui.session import CollectSession

    closed = []
    leader = SimpleNamespace(close_hil=lambda: closed.append("leader"))
    follower = SimpleNamespace(num_dofs=lambda: 7, close_hil=lambda: closed.append("follower"))
    agent = yam_adapter.YamTeleop(leader, follower)
    assert agent.release_leader() is False  # Autonomous handoff still preserves gravity compensation.
    session = CollectSession.__new__(CollectSession)
    session.loop = session.recorder = None
    session.units = [SimpleNamespace(name="left", robot=follower, agent=agent)]
    session._teardown_units()
    assert closed == ["leader", "follower"] and session.units == []


def test_legacy_failed_teardown_retains_handles_for_explicit_retry():
    from yam_abc_reproduce.gui.session import CollectSession

    attempts = []

    def close():
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("SDK close failed")

    unit = SimpleNamespace(name="left", robot=SimpleNamespace(close_hil=close), agent=None)
    session = CollectSession.__new__(CollectSession)
    session.loop = session.recorder = None
    session.units = [unit]
    with pytest.raises(RuntimeError, match="ownership retained"):
        session._teardown_units()
    assert session.units == [unit]
    session._teardown_units()
    assert session.units == []


def test_legacy_cli_help_identifies_supported_station(capsys):
    from yam_abc_reproduce.cli import gui

    with pytest.raises(SystemExit) as exc:
        gui(["--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "Legacy" in output and "yam-workstation" in output
