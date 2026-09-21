"""The ONLY module that imports i2rt. Everything else uses the protocols in
``interface.py``.

Boundary conventions (YAM-ABC-Reproduce side):
  * joint vector = ``[arm_0 ... arm_{n-1}, gripper]``
  * arm joints are radians (pass-through to/from i2rt)
  * gripper is normalized ``[0, 1]`` (0 = closed, 1 = open).

NOTE The follower gripper unit (normalized vs raw) depends on i2rt's gripper driver and should be verified on the
bench. The defaults here are placeholders.
"""

from __future__ import annotations

import time
from threading import Event

import numpy as np

from .interface import RobotInterface, TeleopAgent


def _normalize(raw: float, lo: float, hi: float) -> float:
    if hi == lo:
        return float(np.clip(raw, 0.0, 1.0))
    return float(np.clip((raw - lo) / (hi - lo), 0.0, 1.0))


def _denormalize(norm: float, lo: float, hi: float) -> float:
    return float(lo + np.clip(norm, 0.0, 1.0) * (hi - lo))


# Calibration records the mechanical hard stops. Holding position exactly at a
# stop can keep a DM4310 loaded indefinitely, so normal commands stay slightly
# inside the measured travel. This is about 0.10-0.11 rad on the current station.
GRIPPER_ENDPOINT_MARGIN = 0.02


def _build_yam(
    channel: str,
    arm_type: str,
    gripper_type: str,
    ee_mass: float | None,
    gripper_limits: list[float] | None = None,
    *,
    use_coulomb_friction: bool = False,
):
    """Construct an i2rt YAM robot, converting our string config to i2rt enums.

    ``gripper_limits`` becomes i2rt's ``gripper_limits_override``, which pins the gripper's
    ``[closed, open]`` motor range and sets ``gripper_needs_cal = False``. Left None, a
    gripper declaring ``needs_calibration`` re-measures the range on every construction.
    See ``RobotUnitConfig.gripper_limits``.
    """
    import i2rt.robots.get_robot as get_robot_module
    from i2rt.robots.utils import ArmType, GripperType

    # i2rt <= PR #81 applies a +/-2pi startup correction to every motor,
    # including multi-turn linear grippers.  A saved open endpoint near -5 rad
    # then lands in a different coordinate frame and a hold command can press
    # beyond the physical stop.  Fail before opening CAN unless the exact
    # upstream PR #82 correction is installed by our deployment script.
    if not all(
        hasattr(get_robot_module, helper)
        for helper in (
            "_apply_arm_motor_wrap_offsets",
            "_align_gripper_limits_to_motor_position",
        )
    ):
        raise RuntimeError(
            "unsafe i2rt linear-gripper wrap/turn handling; run "
            "scripts/apply_i2rt_safety_patches.sh before connecting hardware"
        )

    return get_robot_module.get_yam_robot(
        channel=channel,
        arm_type=ArmType.from_string_name(arm_type),
        gripper_type=GripperType.from_string_name(gripper_type),
        ee_mass=ee_mass,
        gripper_limits_override=(
            None if gripper_limits is None else np.asarray(gripper_limits, dtype=float)
        ),
        use_coulomb_friction=use_coulomb_friction,
    )


def _age_from_stamp(stamp: float) -> float:
    age = time.time() - float(stamp)
    if not np.isfinite(age) or age < -0.05:
        raise RuntimeError("invalid motor feedback clock")
    return max(0.0, age)


def _joint_snapshot(robot) -> tuple[np.ndarray, float]:
    """Copy one complete published i2rt state without waiting on its CAN lock.

    i2rt builds a new ``JointStates`` object, then replaces ``_joint_state`` in
    one Python reference assignment; it does not mutate that published object's
    arrays afterwards. A local reference is therefore coherent while the server
    prepares its next state. Timestamp freshness still detects a stopped producer.
    """
    if not robot._server_thread.is_alive():
        raise RuntimeError("i2rt control thread stopped")
    state = robot._joint_state
    if state is None:
        raise RuntimeError("i2rt motor feedback unavailable")
    return np.array(state.pos, dtype=np.float64, copy=True), _age_from_stamp(state.timestamp)


def _feedback_age(robot) -> float:
    # Pinned i2rt timestamps SDK state updates, not individual CAN receipt times.
    return _joint_snapshot(robot)[1]


class YamRobot(RobotInterface):
    """Follower YAM arm wrapped from i2rt's ``get_yam_robot``."""

    def __init__(
        self,
        channel: str,
        gripper_type: str = "linear_4310",
        num_arm_joints: int = 6,
        arm_type: str = "yam",
        ee_mass: float | None = None,
        gripper_limits: list[float] | None = None,
        gripper_raw_open: float = 1.0,
        gripper_raw_closed: float = 0.0,
    ):
        self._robot = _build_yam(channel, arm_type, gripper_type, ee_mass, gripper_limits)
        self._n = num_arm_joints
        self._g_open = gripper_raw_open
        self._g_closed = gripper_raw_closed
        self._gripper_command_margin = GRIPPER_ENDPOINT_MARGIN

    def num_dofs(self) -> int:
        return self._n + 1

    def gripper_limits(self) -> list[float] | None:
        """The ``[closed, open]`` gripper motor range i2rt is using: the pinned config
        value, or what its auto-calibration measured this run. Our normalized ``[0, 1]``
        gripper spans exactly this range, so a collapsed one means the jaws barely move."""
        limits = self._robot.get_robot_info().get("gripper_limits")
        return None if limits is None else [float(x) for x in limits]

    def get_joint_pos(self) -> np.ndarray:
        q = np.asarray(self._robot.get_joint_pos(), dtype=np.float64).reshape(-1)
        arm = q[: self._n]
        grip = _normalize(q[self._n], self._g_closed, self._g_open)
        return np.concatenate([arm, [grip]])

    def hil_read(self) -> tuple[np.ndarray, float]:
        """Low-latency coherent position/age snapshot for the station tick."""
        q, age = _joint_snapshot(self._robot)
        arm = q[: self._n]
        grip = _normalize(q[self._n], self._g_closed, self._g_open)
        return np.concatenate([arm, [grip]]), age

    def command_joint_pos(self, pos: np.ndarray) -> None:
        pos = np.asarray(pos, dtype=np.float64).reshape(-1)
        arm = pos[: self._n]
        margin = getattr(self, "_gripper_command_margin", GRIPPER_ENDPOINT_MARGIN)
        grip = np.clip(
            pos[self._n],
            margin,
            1.0 - margin,
        )
        grip_raw = _denormalize(grip, self._g_closed, self._g_open)
        self._robot.command_joint_pos(np.concatenate([arm, [grip_raw]]))

    def gravity_compensate(self, pos: np.ndarray) -> None:
        """Arm gravity compensation with gripper position retained, control-owner only."""
        if not self._robot.use_gravity_comp:
            raise RuntimeError("SDK gravity compensation is not configured")
        raw = np.asarray(pos, dtype=float).copy()
        margin = getattr(self, "_gripper_command_margin", GRIPPER_ENDPOINT_MARGIN)
        grip = np.clip(
            raw[self._n],
            margin,
            1.0 - margin,
        )
        raw[self._n] = _denormalize(grip, self._g_closed, self._g_open)
        kp = self._robot._kp.copy()
        kd = self._robot._kd.copy()
        kp[:self._n] = 0
        kd[:self._n] = self._robot._grav_comp_kd[:self._n]
        self._robot.command_joint_state({"pos": raw, "vel": np.zeros_like(raw), "kp": kp, "kd": kd})

    def get_observations(self) -> dict[str, np.ndarray]:
        full = self.get_joint_pos()
        return {"joint_pos": full[: self._n].copy(), "gripper_pos": full[self._n :].copy()}

    def stop(self) -> None:
        # Hold current pose (cease following). Used by E-STOP: the arm must stay
        # put (not go limp) so anything held isn't dropped.
        try:
            self._robot.command_joint_pos(self._robot.get_joint_pos())
        except Exception:
            pass

    def relax(self) -> None:
        # Zero torque: the arm goes limp (kp/kd/commands -> 0). Reversible -- a
        # later command_joint_pos + rebuilt kp/kd re-energizes it. Used when
        # tearing the session down so the arm is safe to handle / re-home.
        try:
            self._robot.zero_torque_mode()
        except Exception:
            pass

    def feedback_age(self) -> float:
        return _feedback_age(self._robot)

    def joint_limits(self) -> np.ndarray:
        limits = np.asarray(self._robot.get_robot_info()["joint_limits"], dtype=float)
        return limits[: self._n].copy()

    def close_hil(self):
        """Explicit session teardown; removes active motor control. Support arms first."""
        self._robot.close()

    def power_off(self) -> dict:
        """Send i2rt's motor-off frame to every follower joint and gripper.

        This deliberately differs from :meth:`stop`: ``stop`` holds the last
        pose, whereas this method disables each motor after the control thread
        has stopped.  The caller must therefore ensure the arm is supported.
        """
        chain = self._robot.motor_chain
        motor_interface = chain.motor_interface
        disabled, failed = [], {}
        for spec in chain.motor_list:
            motor_id = int(spec[0])
            try:
                motor_interface.motor_off(motor_id)
                disabled.append(motor_id)
            except Exception as exc:  # noqa: BLE001
                failed[str(motor_id)] = str(exc)
        # Stop i2rt's background command thread only after every motor-off frame
        # has been attempted, so no later position command can re-enable a joint.
        try:
            chain.close()
        except Exception as exc:  # noqa: BLE001
            failed["transport"] = str(exc)
        return {"disabled": disabled, "failed": failed}


class YamLeaderArm:
    """Motorized official YAM lead arm read through i2rt.

    Backdrivability: with ``bilateral_kp == 0`` the PD gains are zeroed so the arm
    floats on i2rt's gravity compensation (exactly as i2rt's bimanual_lead_follower
    leader does). With ``bilateral_kp > 0`` the leader is given proportional gains
    and can be commanded toward the follower pose for force feedback.
    """

    def __init__(
        self,
        channel: str,
        num_arm_joints: int = 6,
        arm_type: str = "yam",
        gripper_type: str = "yam_teaching_handle",
        ee_mass: float | None = None,
        bilateral_kp: float = 0.0,
    ):
        # Official teaching handles contribute their 0.258 kg inertial model.
        # Retain the existing friction aid for powered tracking/hold; manual
        # control below disables it to match the official minimum_gello example.
        self._robot = _build_yam(
            channel,
            arm_type,
            gripper_type,
            ee_mass,
            use_coulomb_friction=True,
        )
        self._n = num_arm_joints
        # Remember the arm's native kp so bilateral scaling is relative to it.
        self._native_kp = np.asarray(getattr(self._robot, "_kp", np.zeros(self._n)), dtype=float)
        self._native_kd = np.asarray(
            getattr(self._robot, "_kd", np.zeros(self._n)), dtype=float
        ).copy()
        self._hil_manual = None
        kp = self._native_kp * bilateral_kp if bilateral_kp > 0 else np.zeros(self._n)
        self._robot.update_kp_kd(kp=kp, kd=np.zeros(self._n))

    def get_state_with_age(self) -> tuple[np.ndarray, float, list[bool], float]:
        """One same-bus read -> (arm_joints, gripper_norm, [top, second] buttons).

        Mirrors i2rt minimum_gello's ``YAMLeaderRobot.get_info``: the teaching-handle
        trigger is ``1 - position`` and ``io_inputs`` are the two button bits."""
        joint_pos, age = _joint_snapshot(self._robot)
        arm = joint_pos[: self._n]
        # The DM thread similarly publishes a new encoder-info list by replacing
        # one reference. Copy scalar fields without waiting behind its next CAN read.
        encoders = self._robot.motor_chain.same_bus_device_states
        if not encoders:
            raise RuntimeError("leader teaching-handle feedback unavailable")
        enc = encoders[0]
        gripper = float(np.clip(1.0 - enc.position, 0.0, 1.0))
        # Teaching-handle button polarity varies between units (some idle high,
        # some idle low). Learn the idle level from the first read (assumes no
        # button held during startup) and report "pressed" as deviation from it.
        raw = [bool(b) for b in enc.io_inputs]
        if (
            not hasattr(self, "_btn_idle")
            or self._btn_idle is None
            or len(self._btn_idle) != len(raw)
        ):
            self._btn_idle = raw
        buttons = [r != i for r, i in zip(raw, self._btn_idle)]
        return arm, gripper, buttons, age

    def get_state(self) -> tuple[np.ndarray, float, list[bool]]:
        arm, gripper, buttons, _age = self.get_state_with_age()
        return arm, gripper, buttons

    def feedback_age(self) -> float:
        return _feedback_age(self._robot)

    def set_manual_control(self, manual: bool, gain_scale: float = 0.2):
        if not 0 < gain_scale <= 1:
            raise ValueError("leader gain scale must be in (0,1]")
        if self._hil_manual == manual and (
            manual or getattr(self, "_hil_gain_scale", None) == gain_scale
        ):
            return
        if manual:
            # Match minimum_gello with bilateral_kp=0: publish zero PD gains,
            # retaining gravity feedforward but not our optional friction aid.
            self._robot.use_coulomb_friction = False
            self._robot.update_kp_kd(kp=np.zeros(self._n), kd=np.zeros(self._n))
            self._robot.command_joint_pos(self._robot.get_joint_pos().copy())
        else:
            self._robot.use_coulomb_friction = True
            current = self._robot.get_joint_pos().copy()
            self._robot.update_kp_kd(kp=self._native_kp * gain_scale, kd=self._native_kd.copy())
            self._robot.command_joint_pos(current)
        self._hil_manual = manual
        self._hil_gain_scale = gain_scale

    def close_hil(self):
        self._robot.close()

    def control_status(self):
        # Idle commands use _grav_comp_kd, not the configured position _kd.
        command = getattr(self._robot, "_commands", None)
        return {
            "manual": self._hil_manual,
            "kp": np.asarray(command.kp).tolist() if command is not None else None,
            "kd": np.asarray(command.kd).tolist() if command is not None else None,
            "friction_compensation": getattr(self._robot, "use_coulomb_friction", None),
        }

    def command_arm(self, arm_joints: np.ndarray) -> None:
        """Command the leader arm joints (bilateral force feedback only)."""
        self._robot.command_joint_pos(np.asarray(arm_joints, dtype=np.float64).reshape(-1))


class YamTeleop(TeleopAgent):
    """Identity map the lead arm onto the follower and apply it each tick.

    Leader and follower are the same YAM geometry, so the mapping is 1:1.
    """

    def __init__(self, leader: YamLeaderArm, follower: YamRobot, bilateral_kp: float = 0.0):
        self._leader = leader
        self._follower = follower
        self._bilateral_kp = bilateral_kp
        self._n = follower.num_dofs() - 1
        # Leader state cached by read_inputs() so act() reads the bus only once/tick.
        self._cached: tuple[np.ndarray, float] | None = None

    def _read_leader(self) -> tuple[np.ndarray, float, list[bool]]:
        arm, gripper, buttons = self._require_leader().get_state()
        self._cached = (arm, gripper)
        return arm, gripper, buttons

    def _require_leader(self):
        if self._leader is None:
            raise RuntimeError(
                "this arm's leader was released for autonomy — Reset Session and "
                "Start Teleop to rebuild it"
            )
        return self._leader

    def _target(self) -> np.ndarray:
        if self._cached is None:
            arm, gripper, _ = self._read_leader()
        else:
            arm, gripper = self._cached
        return np.concatenate([arm, [float(np.clip(gripper, 0.0, 1.0))]])

    def act(self, obs: dict[str, np.ndarray]) -> np.ndarray:
        cmd = self._target()  # identity: follower target == leader pose
        self._follower.command_joint_pos(cmd)
        if self._bilateral_kp > 0:
            # Push the leader toward the follower's current arm pose (haptics).
            self._leader.command_arm(np.asarray(obs["joint_pos"], dtype=np.float64).reshape(-1))
        self._cached = None
        return cmd

    def read_inputs(self) -> tuple[list[bool], float] | None:
        _, gripper, buttons = self._read_leader()
        return buttons, gripper

    def hil_read(self):
        leader = self._require_leader()
        read = getattr(leader, "get_state_with_age", None)
        if callable(read):
            arm, grip, buttons, age = read()
            self._cached = (arm, grip)
        else:
            arm, grip, buttons = self._read_leader()
            age = leader.feedback_age()
        return np.concatenate([arm, [grip]]), buttons, age

    def hil_leader_command(self, joints, *, manual, gain_scale=0.2):
        leader = self._require_leader()
        leader.set_manual_control(manual, gain_scale)
        if not manual:
            leader.command_arm(joints)

    def hil_leader_status(self):
        return self._require_leader().control_status()

    def close_hil(self):
        self._require_leader().close_hil()

    def leader_raw(self) -> tuple[np.ndarray, np.ndarray] | None:
        """``(raw, cal)`` leader joint angles in radians for the live readout, or None for
        a leader without a raw readout (``YamLeaderArm`` reports through i2rt, already
        calibrated)."""
        angles = getattr(self._leader, "leader_angles", None)
        return angles() if callable(angles) else None

    def engage(self, abort: Event | None = None, steps: int = 33, dt: float = 0.03) -> None:
        """Ease the follower from its current pose to the leader's pose before live
        tracking, so it never snaps. Blocks ~steps*dt s (~1 s). Aborts early when
        ``abort`` (the loop's E-STOP event) is set, so a stop interrupts the ramp
        instead of it commanding the follower all the way to the leader."""
        arm, gripper, _ = self._require_leader().get_state()
        target = np.concatenate([arm, [float(np.clip(gripper, 0.0, 1.0))]])
        start = np.asarray(self._follower.get_joint_pos(), dtype=np.float64).reshape(-1)
        for i in range(1, steps + 1):
            if abort is not None and abort.is_set():
                return
            alpha = i / steps
            self._follower.command_joint_pos(start * (1.0 - alpha) + target * alpha)
            time.sleep(dt)

    def release_leader(self) -> bool:
        """Drop the leader's CAN bus while the follower stays energized, so an
        autonomous rollout runs on the follower buses only.

        Returns whether the bus was actually closed: a motorized ``YamLeaderArm`` has
        no ``stop()`` -- closing its i2rt chain would drop gravity compensation and the
        arm would sag -- so it keeps its bus. A passive GELLO has no torque to lose.

        Either way the agent is poisoned, because ``PassiveGelloLeader.stop()`` leaves
        its last sample in place: without this, ``get_state()`` would keep returning a
        frozen pose and a later ``act()`` would snap the follower to wherever the leader
        was at handoff.
        """
        leader, self._leader, self._cached = self._leader, None, None
        leader_stop = getattr(leader, "stop", None)
        if not callable(leader_stop):
            return False
        leader_stop()
        return True

    def stop(self) -> None:
        self._follower.stop()
        # Release the leader too (e.g. a passive GELLO's CAN reader thread/bus).
        leader_stop = getattr(self._leader, "stop", None)
        if callable(leader_stop):
            try:
                leader_stop()
            except Exception:
                pass
