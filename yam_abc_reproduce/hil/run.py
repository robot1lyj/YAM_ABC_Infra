"""YAM four-mode workstation: uv run --no-sync yam-workstation --mock.

Keyboard: s start/resume, i HIL takeover, space hold, 1/2/3/4 select mode, r collection segment,
q quit (hardware shutdown removes active motor control; support the arms first).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import queue
import threading
import time
from contextlib import nullcontext
from importlib.util import find_spec
from pathlib import Path

import numpy as np

from ..camera.worker import CameraWorker
from ..config import (
    build_station_config,
    controller_channel_for,
    load_yaml,
    robot_channel_for,
)
from ..resource_qos import place_on_cpus
from ..robot.can_bus import bring_up_can_buses, check_can_up, stop_can_buses
from ..runtime import build_arm_units, build_cameras_from_config
from .buttons import HandleButtons
from .core import Arbiter, Mode, Phase
from .jog import Jog
from .maintenance import Maintenance
from .metrics import Latencies
from .observation import Observations
from .policy import PlainPolicyClient, PolicyWorker
from .recording import RecordingSession
from .session import Session
from .station import StationIO


class MockPolicy:
    def infer(self, obs):
        time.sleep(0.08)
        actions = np.tile(obs["observation.state"], (50, 1))
        actions[:, [0, 7]] += 0.04 * np.sin(np.arange(50)[:, None] / 15)
        return {"actions": actions}


def station_can_channels(cfg) -> list[str]:
    """CAN interfaces owned by a full four-arm workstation session."""
    robots = cfg.robot.robots or [cfg.robot.active_robot()]
    channels = [robot_channel_for(robot) for robot in robots]
    channels += [controller_channel_for(cfg.robot.controller_for(robot)) for robot in robots]
    return list(dict.fromkeys(channels))


def prepare_station_can(cfg) -> str:
    """Bring station CAN up and verify every interface before i2rt opens it."""
    needed = station_can_channels(cfg)
    down = check_can_up(needed)
    if down:
        errors = bring_up_can_buses(down)
        if errors:
            raise RuntimeError("CAN setup failed before arm connection: " + "; ".join(errors))
        down = check_can_up(needed)
    if down:
        raise RuntimeError(
            "CAN interface(s) not up after bring-up: "
            + ", ".join(down)
            + ". Check the USB-CAN adapters or use the explicit Reset CAN recovery action."
        )
    return "CAN ready without reset: " + ", ".join(needed)


class LocalEdgePolicy:
    def __init__(self, url, timeout=1.5):
        self.url, self.client = url, None
        self.timeout = timeout

    def infer(self, obs):
        if self.client is None:
            self.client = PlainPolicyClient(self.url, timeout=self.timeout)
        return self.client.infer(obs)

    @property
    def metadata(self):
        return None if self.client is None else self.client.metadata

    def close(self):
        if self.client:
            self.client.close()


class Runtime:
    def __init__(
        self,
        io,
        cameras,
        worker,
        recorder,
        *,
        mode="hil",
        hz=30,
        action_dt=1 / 30,
        streaming=True,
        prompt="pick and place",
        settings=None,
    ):
        self.io, self.cameras, self.worker, self.recorder = io, cameras, worker, recorder
        self.hz, self.prompt = hz, prompt
        settings = settings or {}
        self.max_state_age = settings.get("max_state_age", 0.25)
        self.mirror_error = settings.get("mirror_error", 0.5)
        self.max_frame_age = settings.get("max_frame_age", 0.5)
        self.observations = Observations(
            cameras,
            max_age=self.max_frame_age,
            max_skew=settings.get("max_frame_skew", 0.12),
            warn_skew=settings.get("warn_frame_skew", 0.04),
        )
        self.session = Session(
            Arbiter(
                Mode(mode),
                streaming=streaming,
                action_dt=action_dt,
                max_request_age=settings.get("request_timeout", 1.5),
                max_action_age=settings.get("action_timeout", 1.5),
                tick_timeout=settings.get("tick_timeout", 0.5),
                handover_error=settings.get("handover_error", 0.2),
                max_joint_speed=settings.get("max_joint_speed", 5.0),
                max_manual_joint_speed=settings.get("max_manual_joint_speed"),
                max_manual_gripper_speed=settings.get("max_manual_gripper_speed"),
                replan_period=settings.get("replan_period", 0.2),
            ),
            worker,
        )
        self.events = queue.Queue(maxsize=16)
        self.takeovers = queue.Queue(maxsize=1)
        self.intervention_id = 0
        self.stopping = threading.Event()
        self.holding = queue.Queue(maxsize=1)
        self.status = {"phase": "hold", "mode": mode, "tick": 0}
        self.handle_buttons = HandleButtons()
        self.outcome = "unknown"
        self.latencies = Latencies()
        self.emergency = threading.Event()
        self.jog = Jog()
        self.maintenance = Maintenance()
        self.operator_error = None
        self.recording_error = None
        self._record_started = None
        self.recording_allowed = True

    def _recording_failed(self, state, reason):
        """Abort the episode and signal HOLD without latching a motor fault."""
        if self.recording_error is not None:
            return
        self.recording_error = str(reason)
        self.outcome = "aborted"
        self.recorder.metadata["recording_error"] = self.recording_error
        self.recorder.abort_episode()
        self.session.arbiter.hold(state)
        hold_errors = self.io.hold()
        if hold_errors:
            raise RuntimeError("hold after recording failure: " + "; ".join(hold_errors))

    def event(self, event):
        allowed = {
            "start",
            "stop",
            "reset_stop",
            "home",
            "capture_home",
            "gravity",
            "takeover",
            "resume_policy",
            "discard",
            "hold",
            "quit",
            "mode:teleop",
            "mode:inference",
            "mode:hil",
            "mode:collect",
            "record",
            "success",
            "failure",
        }
        if event not in allowed:
            raise ValueError("unknown event")
        if event in ("record", "discard") and not self.recording_allowed:
            raise ValueError("standalone teleoperation does not record data")
        if self.recording_error and not (
            event in ("hold", "stop", "quit", "reset_stop", "mode:teleop")
            or (event == "start" and self.status.get("mode") == "teleop")
        ):
            raise ValueError("录制已中断；仅可切换为不录制的遥操作或断开机械臂")
        if self.maintenance.latched and event not in ("stop", "hold", "quit", "reset_stop"):
            raise ValueError("紧急暂停已锁存，请先检查现场并解除锁存")
        if event in ("home", "capture_home", "gravity"):
            if self.maintenance.state != "idle":
                raise ValueError("请先结束当前维护操作")
            if self.status.get("phase") != "hold" or getattr(self.recorder, "recording", False):
                raise ValueError("请先暂停并结束录制，再进行设备维护")
            if event == "home" and self.maintenance.ready is None:
                raise ValueError("尚未保存准备位")
        if event in ("mode:inference", "mode:hil") and self.worker is None:
            raise ValueError("restart with --url to enable local policy inference")
        if event in ("record", "discard") and self.status.get("mode") != "collect":
            raise ValueError("record control is only available in data collection mode")
        if (
            event == "record"
            and not getattr(self.recorder, "recording", False)
            and self.status.get("phase") != "human"
        ):
            raise ValueError("start leader teleoperation before recording")
        if event == "stop":
            self.emergency.set()
        elif event == "quit":
            self.stopping.set()
        elif event == "hold":
            try:
                self.holding.put_nowait(time.monotonic())
            except queue.Full:
                pass
        elif event == "takeover":
            try:
                self.takeovers.put_nowait((event, time.monotonic()))
            except queue.Full:
                pass
        else:
            self.events.put_nowait((event, time.monotonic()))

    def request_jog(self, arm, joint, delta):
        if self.recording_error:
            raise ValueError("录制已中断；请先断开机械臂，再进入设备调试")
        if self.status.get("mode") != "collect" or self.status.get("phase") != "hold":
            raise ValueError("关节点动仅在采集模式暂停状态可用")
        if (
            getattr(self.recorder, "recording", False)
            or self.emergency.is_set()
            or self.maintenance.latched
            or self.maintenance.state != "idle"
        ):
            raise ValueError("录制或紧急暂停时不可点动")
        self.jog.request(arm, joint, delta)

    def run(self, *, duration=None, auto_start=False, demo=False):
        period = 1 / self.hz
        start = last = deadline = time.monotonic()
        tick, missed = 0, 0
        demo_stage = 0
        last_obs_at = None
        last_record_images = {}
        try:
            place_on_cpus("CONTROL")
            while not self.stopping.is_set():
                now = time.monotonic()
                elapsed = now - start
                if duration is not None and elapsed >= duration:
                    break
                dt = max(period, now - last)
                last = now
                q, leader, buttons, ages = self.io.read()
                read_done = time.monotonic()
                if max(ages) > self.max_state_age:
                    raise RuntimeError("SDK state update stale")
                self.observations.add_state(now, q)
                snapshot = self.observations.snapshot(now, self.prompt)
                observation_done = time.monotonic()
                if isinstance(self.recorder, RecordingSession) and self.recorder.error:
                    self._recording_failed(q, self.recorder.error)
                try:
                    event, requested_at = self.events.get_nowait()
                except queue.Empty:
                    event, requested_at = None, None
                a = self.session.arbiter
                button_event = self.handle_buttons.read(
                    buttons, now=now, mode=a.mode, phase=a.phase
                )
                if button_event in ("record", "discard") and not self.recording_allowed:
                    button_event = None
                if self.recording_error and button_event in (
                    "record", "discard", "success", "failure"
                ):
                    button_event = None
                try:
                    hold_time = self.holding.get_nowait()
                except queue.Empty:
                    hold_time = None
                hold_requested = hold_time is not None
                try:
                    urgent, urgent_at = self.takeovers.get_nowait()
                except queue.Empty:
                    urgent, urgent_at = None, None
                if button_event:
                    event, requested_at = button_event, now
                if urgent and a.mode == Mode.HIL and a.phase in (Phase.POLICY, Phase.RESUME):
                    event, requested_at = urgent, urgent_at
                    while not self.events.empty():
                        self.events.get_nowait()
                if auto_start and snapshot is not None and demo_stage == 0:
                    event, demo_stage = "start", 1
                if demo and demo_stage == 1 and elapsed > 1:
                    event, demo_stage = "takeover", 2
                elif demo and demo_stage == 2 and elapsed > 2:
                    event, demo_stage = "resume_policy", 3
                if hold_requested:
                    while not self.events.empty():
                        self.events.get_nowait()
                    while not self.takeovers.empty():
                        self.takeovers.get_nowait()
                    event, requested_at = "hold", hold_time
                if self.emergency.is_set():
                    event, requested_at = "stop", now
                    self.emergency.clear()
                    while not self.events.empty():
                        self.events.get_nowait()
                    while not self.takeovers.empty():
                        self.takeovers.get_nowait()
                if (
                    self.recording_error
                    and self.session.arbiter.mode != Mode.TELEOP
                    and event is not None
                    and event not in ("stop", "hold", "reset_stop", "mode:teleop")
                ):
                    # An old collection command must not interrupt live teleoperation.
                    event, requested_at = None, None
                obs_id, observed_at, obs, images, quality = (
                    (0, None, None, {}, {}) if snapshot is None else snapshot
                )
                if snapshot is not None:
                    last_obs_at = observed_at
                # A short pairing miss is diagnostic; prolonged loss holds policy.
                fresh = last_obs_at is not None and now - last_obs_at <= self.max_frame_age
                joints = np.ones(14, dtype=bool)
                joints[[6, 13]] = False
                error = float(np.max(np.abs(q[joints] - leader[joints])))
                a = self.session.arbiter
                if (
                    a.mode == Mode.HIL
                    and a.phase == Phase.POLICY
                    and error > self.mirror_error
                    and event != "takeover"
                ):
                    event = "hold"
                recording_event = None
                if event in ("success", "failure"):
                    if a.phase != Phase.FAULT:
                        self.outcome = event
                    event = None
                if event in ("record", "discard"):
                    recording_event, event = event, None
                original_event = event
                if event in ("home", "capture_home", "gravity") and (
                    a.phase != Phase.HOLD
                    or getattr(self.recorder, "recording", False)
                    or self.maintenance.state != "idle"
                    or self.maintenance.latched
                ):
                    self.operator_error = "设备状态已变化，维护指令未执行；请先暂停并结束录制"
                    event = None
                elif event is not None:
                    self.operator_error = None
                if event in ("stop", "home", "gravity", "capture_home", "reset_stop", "hold"):
                    if isinstance(self.recorder, RecordingSession):
                        self.recorder.stop_episode(
                            "aborted"
                            if event == "stop"
                            or (event == "hold" and a.mode in (Mode.COLLECT, Mode.TELEOP))
                            else self.outcome
                        )
                event = self.maintenance.command(
                    event,
                    q,
                    leader,
                    now=now,
                    paused=(
                        a.phase == Phase.HOLD
                        and not getattr(self.recorder, "recording", False)
                        and not self.maintenance.latched
                        and self.maintenance.state == "idle"
                    ),
                )
                previous_phase = a.phase
                decision = self.session.tick(
                    q,
                    leader,
                    now=now,
                    dt=dt,
                    observation_id=obs_id,
                    observation=obs,
                    observed_at=observed_at,
                    fresh=fresh,
                    leader_ready=(a.mode == Mode.INFERENCE or error <= a.handover_error),
                    event=event,
                )
                maintenance_action = self.maintenance.step(q, leader, now=now, dt=period)
                jog_action = self.jog.step(
                    q,
                    allowed=(
                        event is None
                        and a.mode == Mode.COLLECT
                        and a.phase == Phase.HOLD
                        and not getattr(self.recorder, "recording", False)
                        and not self.maintenance.latched
                        and self.maintenance.state == "idle"
                    ),
                    now=now,
                    dt=period,
                )
                if jog_action is not None:
                    decision.action = jog_action
                    decision.selected_action = jog_action.copy()
                if maintenance_action is not None:
                    decision.action = maintenance_action[0]
                    decision.selected_action = maintenance_action[0].copy()
                if self.maintenance.state == "gravity":
                    decision.action[[6, 13]] = self.maintenance.grippers
                    decision.selected_action = decision.action.copy()
                # Never enlarge a motion step because this loop missed its deadline.
                joint_speed = a.max_joint_speed
                if decision.phase == Phase.HUMAN and a.max_manual_joint_speed is None:
                    joint_speed = np.inf
                elif decision.phase == Phase.HUMAN:
                    joint_speed = a.max_manual_joint_speed
                limits = np.full(14, joint_speed * period)
                gripper_speed = a.max_gripper_speed
                if decision.phase == Phase.HUMAN and a.max_manual_gripper_speed is None:
                    gripper_speed = np.inf
                elif decision.phase == Phase.HUMAN:
                    gripper_speed = a.max_manual_gripper_speed
                limits[[6, 13]] = gripper_speed * period
                decision.action = np.clip(decision.action, q - limits, q + limits)
                decision_done = time.monotonic()
                submitted, stamps = self.io.apply(
                    decision,
                    q,
                    leader,
                    dt=period,
                    mirror=a.mode == Mode.HIL,
                    **(
                        {"maintenance_leader": maintenance_action[1]}
                        if maintenance_action is not None
                        else {}
                    ),
                    **({"gravity": True} if self.maintenance.state == "gravity" else {}),
                )
                if (
                    jog_action is not None
                    or maintenance_action is not None
                    or self.maintenance.state == "gravity"
                ):
                    # Preserve the actual clamped target; never spring back after a jog.
                    a.hold(submitted)
                apply_done = time.monotonic()
                transitions = []
                if a.phase == Phase.TAKEOVER and previous_phase != Phase.TAKEOVER:
                    self.intervention_id += 1
                    transitions.append("takeover_applied")
                if a.phase == Phase.HUMAN and previous_phase == Phase.TAKEOVER:
                    transitions.append("human_started")
                if a.phase == Phase.RESUME and previous_phase == Phase.HUMAN:
                    transitions.append("resume_requested")
                if a.phase == Phase.POLICY and previous_phase != Phase.POLICY:
                    transitions.append("policy_started")
                row = {
                    "event_requested_at": requested_at,
                    "event_applied_at": apply_done if transitions else None,
                    "transitions": transitions,
                    "intervention_id": self.intervention_id,
                    "tick": tick,
                    "policy_reply": self.session.last_reply,
                    "time": now,
                    "mode": a.mode.value,
                    "phase": decision.phase.value,
                    "event": original_event,
                    "maintenance": self.maintenance.state,
                    "stop_latched": self.maintenance.latched,
                    "epoch": decision.epoch,
                    "source": decision.source,
                    "is_intervention": decision.intervention,
                    "expert_valid": decision.source == "human" and snapshot is not None,
                    "policy_valid": decision.policy_valid,
                    "policy_action": decision.policy_action,
                    "human_action": leader if decision.source == "human" else None,
                    "selected_action": decision.selected_action,
                    "submitted_action": submitted,
                    "constraint_mask": np.abs(submitted - decision.selected_action) > 1e-8,
                    "measured_state": q,
                    "leader_state": leader,
                    "gripper_owned": decision.gripper_owned,
                    "observation_state": None if obs is None else obs["observation.state"],
                    "obs_id": obs_id,
                    "sync": quality,
                    "sdk_state_age_s": ages,
                    "request": None
                    if decision.request is None
                    else dataclasses.asdict(decision.request),
                    "action_index": decision.action_index,
                    "submitted_at": stamps,
                }
                if isinstance(self.recorder, RecordingSession):
                    if (
                        original_event == "start"
                        and a.phase != Phase.HOLD
                        and a.mode in (Mode.HIL, Mode.INFERENCE)
                        and self.recording_allowed
                    ):
                        self.recorder.start_episode()
                    previous_mode = self.recorder.mode
                    self.recorder.set_mode(a.mode.value, self.outcome)
                    if previous_mode != a.mode.value:
                        self.outcome = "unknown"
                    if a.mode == Mode.COLLECT:
                        if recording_event == "discard":
                            self.recorder.stop_episode("discarded")
                        elif event == "hold" or a.phase == Phase.FAULT:
                            self.recorder.stop_episode("aborted")
                        elif recording_event:
                            if self.recorder.recording:
                                self.recorder.stop_episode(self.outcome)
                            elif a.phase == Phase.HUMAN and snapshot is not None:
                                self.outcome = "unknown"
                                self.recorder.start_episode()
                        row["record_event"] = recording_event
                if images:
                    last_record_images = images
                row["observation_valid"] = snapshot is not None
                if not self.recorder.submit(row, images or last_record_images):
                    if isinstance(self.recorder, RecordingSession):
                        self._recording_failed(q, self.recorder.error or "recorder unavailable")
                    else:
                        raise RuntimeError(self.recorder.error or "recorder unavailable")
                submitted_done = time.monotonic()
                performance = self.latencies.add(
                    now,
                    io_read=read_done - now,
                    observation=observation_done - read_done,
                    arbitration=decision_done - observation_done,
                    io_apply=apply_done - decision_done,
                    record_submit=submitted_done - apply_done,
                    control_work=submitted_done - now,
                    tick_interval=dt,
                    **{
                        f"io_{name}": seconds
                        for name, seconds in getattr(self.io, "read_timings_s", {}).items()
                    },
                )
                if getattr(self.recorder, "recording", False):
                    if self._record_started is None:
                        self._record_started = now
                else:
                    self._record_started = None
                self.status = {
                    "episode_elapsed_s": 0
                    if self._record_started is None
                    else now - self._record_started,
                    "stop_latched": self.maintenance.latched,
                    "maintenance": self.maintenance.state,
                    "maintenance_error": self.maintenance.error,
                    "operator_error": self.operator_error,
                    "ready_pose": self.maintenance.ready,
                    "ready_version": self.maintenance.ready_version,
                    "updated_at": time.monotonic(),
                    "follower_state": q.tolist(),
                    "leader_state": leader.tolist(),
                    "sdk_state_age_s": list(ages),
                    "gripper_limits": [
                        getattr(unit.robot, "gripper_limits", lambda: None)()
                        for unit in getattr(self.io, "units", [])
                    ],
                    "buttons": buttons,
                    "jog_active": self.jog.target is not None,
                    "performance": performance,
                    "record_metrics": getattr(self.recorder, "metrics", {}),
                    "tick": tick,
                    "mode": a.mode.value,
                    "phase": a.phase.value,
                    "source": decision.source,
                    "leader_error_rad": error,
                    "frame_age_s": quality.get("age_s"),
                    "arrival_skew_s": quality.get("arrival_skew_s"),
                    "deadline_misses": missed,
                    "record_queue": self.recorder.queue.qsize(),
                    "recorded_steps": self.recorder.written,
                    "intervention_id": self.intervention_id,
                    "recording": getattr(self.recorder, "recording", True),
                    "recording_saving": getattr(self.recorder, "saving", False),
                    "recording_error": self.recording_error,
                    "error": a.fault_reason,
                    "outcome": self.outcome,
                }
                if a.phase == Phase.FAULT:
                    self.outcome = "aborted"
                    break
                tick += 1
                deadline += period
                remaining = deadline - time.monotonic()
                if remaining < 0:
                    missed += 1
                    deadline = time.monotonic()
                else:
                    self.stopping.wait(remaining)
        except Exception as exc:
            self.outcome = "aborted"
            self.status = dict(self.status, phase="fault", error=f"{type(exc).__name__}: {exc}")
        finally:
            hold_errors = self.io.hold()
            if hold_errors:
                self.status = dict(self.status, hold_errors=hold_errors)
            if isinstance(self.recorder, RecordingSession) and self.recorder.mode == "collect":
                self.recorder.stop_episode("aborted")
            self.recorder.metadata["terminal_status"] = dict(self.status)
            if self.worker:
                self.recorder.metadata["policy_metadata"] = getattr(
                    self.worker.client, "metadata", None
                )
            # A hardware fault keeps gravity/hold active until explicit quit.
            if self.status.get("phase") == "fault" and not self.io.mock:
                print(
                    "Fault latched; arms held where possible. Support arms, then q to shut down.",
                    flush=True,
                )
                while not self.stopping.wait(0.1):
                    pass
            self.status = dict(self.status, running=False)
        return self.status


def validate_station(cfg, *, mock, check_cameras=True):
    if mock:
        return
    if cfg.robot.num_arm_joints != 6 or {r.type for r in cfg.robot.robots} != {
        "yam_left",
        "yam_right",
    }:
        raise ValueError("station must have two standard six-joint YAM followers")
    if len(cfg.robot.controllers) != 2 or {c.type for c in cfg.robot.controllers} != {
        "yam_lead_left",
        "yam_lead_right",
    }:
        raise ValueError("use official motorized YAM leaders, not GELLO")
    for r in cfg.robot.robots:
        if r.gripper not in ("linear_4310", "linear_3507"):
            raise ValueError("set the verified parallel gripper motor type in the station YAML")
        if cfg.robot.controller_for(r).controls != r.type:
            raise ValueError("invalid leader/follower mapping")
    if not check_cameras:
        return
    if len(cfg.cameras) != 3 or {c.role for c in cfg.cameras} != {"top", "left", "right"}:
        raise ValueError("configure three D405 camera roles")
    serials = [c.serial for c in cfg.cameras]
    if any(not s or s.startswith("REPLACE") for s in serials) or len(set(serials)) != 3:
        raise ValueError("set three distinct actual D405 serials")


def main(argv=None, *, service=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--station", default="configs/station_hil.yaml")
    p.add_argument("--mock", action="store_true")
    p.add_argument(
        "--check", action="store_true", help="仅检查配置和依赖，不连接设备或创建数据目录"
    )
    p.add_argument("--mode", choices=[m.value for m in Mode], default="hil")
    p.add_argument("--url", help="Thor WebSocket URL on the local Ethernet link")
    p.add_argument("--output", type=Path)
    p.add_argument("--raw-only", action="store_true", help="兼容旧命令；采集现在默认只保存原始数据")
    p.add_argument("--segment-seconds", type=float, default=60, help="采集文件分段时长，不拆逻辑集")
    p.add_argument("--min-free-gb", type=float, default=0.5, help="录制保留空间 GiB")
    p.add_argument("--duration", type=float)
    p.add_argument("--demo", action="store_true", help="mock only: automated takeover/resume")
    p.add_argument("--baseline", action="store_true", help="ordinary non-prefetch baseline")
    p.add_argument("--web-port", type=int, help="optional local dashboard port")
    p.add_argument(
        "--web-host",
        default="127.0.0.1",
        help="dashboard listen address; use the IPC LAN address for direct workstation access",
    )
    p.add_argument(
        "--web-allowed-host",
        action="append",
        default=[],
        help="additional HTTP Host name accepted by the dashboard (repeatable)",
    )
    args = p.parse_args(argv)
    if not np.isfinite(args.segment_seconds) or args.segment_seconds <= 0:
        p.error("segment-seconds must be finite and positive")
    if not np.isfinite(args.min_free_gb) or args.min_free_gb < 0:
        p.error("min-free-gb must be finite and nonnegative")
    if args.web_port is not None and not 1 <= args.web_port <= 65535:
        p.error("web-port must be between 1 and 65535")
    if args.web_host != "127.0.0.1" and args.web_port is None:
        p.error("web-host requires web-port")
    if args.web_host == "0.0.0.0" and not args.web_allowed_host:
        p.error("0.0.0.0 requires at least one explicit --web-allowed-host")
    if args.web_port and not args.check and not args.demo and service is None:
        from .workbench import serve

        return serve(args)
    if args.demo and not args.mock:
        p.error("--demo is mock only")
    if args.duration is not None and (not np.isfinite(args.duration) or args.duration <= 0):
        p.error("duration must be finite and positive")
    if args.web_port is not None and not 1 <= args.web_port <= 65535:
        p.error("web-port must be between 1 and 65535")
    cfg = build_station_config(args.station)
    validate_station(cfg, mock=args.mock, check_cameras=service is None)
    hil_cfg = load_yaml(args.station).get("hil", {})
    action_dt = float(hil_cfg.get("action_dt", 1 / 30))
    if not np.isfinite(action_dt) or action_dt <= 0 or not 1 <= cfg.control_hz <= 100:
        p.error("invalid action_dt/control_hz")
    if not args.mock and args.mode not in ("teleop", "collect") and not args.url:
        p.error("--url is required for local edge inference")
    for key, value in hil_cfg.items():
        if not isinstance(value, (float, int)) or not np.isfinite(value) or value <= 0:
            p.error(f"invalid hil setting: {key}")
    # Check before constructing cameras, motors, sockets or recording threads.
    required = {"numpy", "yaml", "av", "h5py"}
    if args.web_port:
        required.update(("fastapi", "uvicorn", "cv2"))
    if not args.mock:
        required.update(("i2rt", "pyrealsense2", "cv2"))
        if args.url:
            required.update(("openpi_client", "websockets", "msgpack"))
    missing = sorted(name for name in required if find_spec(name) is None)
    if missing:
        p.error(
            "缺少依赖 "
            + ", ".join(missing)
            + "；请执行 uv sync --locked --extra camera --extra gui --extra deploy"
        )
    if args.check:
        print(
            json.dumps(
                {
                    "configuration": "ok",
                    "dependencies": sorted(required),
                    "mock": args.mock,
                    "mode": args.mode,
                    "hardware_checked": False,
                    "note": "仅检查模块是否可发现；未验证二进制加载、设备、网络或实时性能",
                },
                ensure_ascii=False,
            )
        )
        return
    # The headless entrypoint also places SDK helper threads before constructing
    # any arm or recording worker; Workbench does the same in its owner thread.
    place_on_cpus("CONTROL")
    output = args.output or Path(cfg.save_root) / time.strftime("hil_%Y%m%d_%H%M%S")
    recorder = RecordingSession(
        output,
        mode=args.mode,
        segment_seconds=args.segment_seconds,
        min_free_bytes=int(args.min_free_gb * 1024**3),
        fps=cfg.control_hz,
        video_backend=None if service is None else service.video_backend,
        metadata={
            "station": dataclasses.asdict(cfg),
            "mock": args.mock,
            "rtc": False,
            "streaming": not args.baseline,
            "action_dt": action_dt,
            "policy_url": args.url,
        },
    )
    workers, io, policy_worker, dashboard = [], None, None, None
    units = []
    units_closed = False
    cameras = []
    try:
        if service is None:
            # Headless sessions own their cameras; GUI connections are independent.
            cameras = build_cameras_from_config(cfg, mock=args.mock)
            for camera in cameras:
                worker = CameraWorker(camera)
                workers.append(worker)
                worker.start()
        else:
            workers = list(service.camera_slots)
        if not args.mock:
            can_output = prepare_station_can(cfg)
            print(f"CAN ready: {can_output}", flush=True)
            print(
                "Opening four YAM arms: motors may energize and grippers may calibrate. Keep leader buttons released.",
                flush=True,
            )
        units = build_arm_units(cfg, mock=args.mock)
        io = StationIO(
            units,
            mock=args.mock,
            leader_gain=hil_cfg.get("leader_gain", 0.2),
            leader_speed=hil_cfg.get("leader_speed", 0.5),
        )
        client = (
            MockPolicy()
            if args.mock
            else LocalEdgePolicy(args.url, timeout=hil_cfg.get("request_timeout", 1.5))
            if args.url
            else None
        )
        policy_worker = PolicyWorker(client) if client else None
        runtime = Runtime(
            io,
            workers,
            policy_worker,
            recorder,
            mode=args.mode,
            hz=cfg.control_hz,
            action_dt=action_dt,
            streaming=not args.baseline,
            prompt=cfg.task_name,
            settings=hil_cfg,
        )
        if service is not None:
            service.attach(runtime, output)
        if args.web_port:
            from .web import start_dashboard

            dashboard = start_dashboard(runtime, args.web_port)
        from .keyboard import Keyboard

        print(
            "s=start, i=takeover, handle 1=resume policy, space=hold, 1/2/3/4=mode, r=record segment, q=shutdown (support arms first)",
            flush=True,
        )
        with Keyboard(runtime.event) if service is None else nullcontext():
            result = runtime.run(duration=args.duration, auto_start=args.demo, demo=args.demo)
        print(json.dumps(result), flush=True)
        # Release motor control before a potentially slow encoder drain. On a
        # recording fault this prevents joints or grippers remaining energized
        # while queued MP4/HDF5 data is finalized.
        if io:
            close_errors = io.close()
            io = None
            units_closed = True
            if close_errors:
                recorder.metadata["close_errors"] = close_errors
                if service is not None:
                    service.cleanup_error = "; ".join(close_errors)
        recorder.close(runtime.outcome)
        if recorder.error and runtime.recording_error is None:
            runtime.recording_error = recorder.error
        if result.get("phase") == "fault":
            raise RuntimeError(result.get("error") or "hardware session aborted")
    finally:
        if dashboard:
            dashboard.should_exit = True
        if io:
            close_errors = io.close()
            units_closed = True
            if close_errors:
                recorder.metadata["close_errors"] = close_errors
                if service is not None:
                    service.cleanup_error = "; ".join(close_errors)
        elif not units_closed:
            for unit in units:
                for device in (unit.agent, unit.robot):
                    close = getattr(device, "close_hil", None)
                    if close:
                        close()
        if service is None:
            for worker in workers:
                worker.stop()
        for camera in cameras[len(workers) :]:
            camera.stop()
        if policy_worker:
            policy_worker.close()
        if recorder._thread.is_alive():
            recorder.close("aborted")
        if not args.mock:
            can_errors = stop_can_buses(station_can_channels(cfg))
            if can_errors:
                message = "CAN shutdown failed: " + "; ".join(can_errors)
                recorder.metadata.setdefault("close_errors", []).append(message)
                if service is not None:
                    service.cleanup_error = message

    if service is not None:
        service.saved()


if __name__ == "__main__":
    main()
