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
from collections import deque
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
from .action_buffer import ActionBuffer
from .buttons import HandleButtons
from .core import Arbiter, Mode, Phase
from .intervention_recording import InterventionRecordingGate
from .jog import Jog
from .maintenance import Maintenance
from .metrics import Latencies
from .observation import Observations
from .planned_buffer import PlannedActionBuffer
from .planner_process import ProcessActionPlanner
from .policy import PolicyWorker, RtcJob
from .policy_process import ProcessPolicyClient
from .recording import RecordingSession
from .recording_service import RemoteRecordingSession
from .rtc_timeline import RtcTimeline
from .session import Session
from .station import StationIO
from .tda_buffer import TdaActionBuffer


class MockPolicy:
    def set_rtc(self, enabled):
        self.rtc = bool(enabled)

    def infer(self, obs):
        time.sleep(0.08)
        actions = np.tile(obs["observation.state"], (50, 1))
        actions[:, [0, 7]] += 0.04 * np.sin(np.arange(50)[:, None] / 15)
        return {"actions": actions}

    def infer_rtc(self, obs, *, target_start_tick, committed_actions):
        result = self.infer(obs)
        result["actions"][:len(committed_actions)] = committed_actions
        result["server_timing"] = {"rtc_used": True}
        return result


RECORDING_SESSIONS = (RecordingSession, RemoteRecordingSession)


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
        self.rtc_delay_steps = settings.get("rtc_delay_steps", 9)
        if type(self.rtc_delay_steps) is not int or not 1 <= self.rtc_delay_steps <= 10:
            raise ValueError("RTC delay must be an integer from 1 to 10 policy ticks")
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
                replan_period=settings.get("replan_period", 0.2),
                expected_policy_latency=settings.get("expected_policy_latency", 0.2),
                prefetch_margin=settings.get("prefetch_margin", 2 / 30),
                policy_fusion=settings.get("policy_fusion", "tda_smooth"),
                external_planner=worker is not None and worker.planner is not None,
                rtc_delay_steps=self.rtc_delay_steps,
            ),
            worker,
            rtc_limit_target=getattr(io, "limit_policy_target", None),
        )
        if self.session.arbiter.rtc_timeline is not None and (
            getattr(io, "policy_trajectory_hz", 0) > 0
            or not callable(getattr(io, "limit_policy_target", None))
            or hz != 30 or abs(action_dt - 1 / 30) > 1e-6
        ):
            raise ValueError("RTC requires 30 Hz direct SDK writes and shared hard limits")
        if worker is not None and worker.planner is not None:
            worker.plan_context = self._plan_context
        self.events = queue.Queue(maxsize=16)
        self.policy_commands = queue.Queue(maxsize=4)
        self.task_switching = False
        self.takeovers = queue.Queue(maxsize=1)
        self.intervention_id = 0
        self.intervention_recording = InterventionRecordingGate()
        self.stopping = threading.Event()
        self.holding = queue.Queue(maxsize=1)
        self.status = {"phase": "hold", "mode": mode, "tick": 0}
        self.handle_buttons = HandleButtons()
        self.outcome = "unknown"
        self.latencies = Latencies()
        self.emergency = threading.Event()
        self.jog = Jog()
        self.maintenance = Maintenance(
            factory_zero=bool(settings.get("factory_zero_home", False)) and not io.mock
        )
        self.operator_error = None
        self.recording_error = None
        self._record_started = None
        self.recording_allowed = True

    def _plan_context(self):
        arbiter = self.session.arbiter
        return (
            arbiter.action_buffer.fusion,
            arbiter.action_buffer.snapshot(),
            arbiter.action_dt,
            arbiter.max_action_age,
        )

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
        if self.task_switching and event not in ("stop", "hold", "quit"):
            raise ValueError("任务切换中，请稍候")
        allowed = {
            "start",
            "stop",
            "reset_stop",
            "home",
            "home_leader",
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
        if event == "start" and self.session.arbiter.intervention_pending:
            raise ValueError("DAgger介入尚未结束，请选择暂停或交还模型")
        if event in ("record", "discard") and not self.recording_allowed:
            raise ValueError("standalone teleoperation does not record data")
        if self.recording_error and not (
            event
            in (
                "hold",
                "stop",
                "quit",
                "reset_stop",
                "home",
                "home_leader",
                "gravity",
                "mode:teleop",
            )
            or (event == "start" and self.status.get("mode") == "teleop")
        ):
            raise ValueError("录制已中断；可进行设备恢复、切换为遥操作或断开机械臂")
        if self.maintenance.latched and event not in ("stop", "hold", "quit", "reset_stop"):
            raise ValueError("紧急暂停已锁存，请先检查现场并解除锁存")
        if event in ("home", "home_leader", "capture_home", "gravity"):
            if event == "home_leader" and not self.maintenance.factory_zero:
                raise ValueError("Leader独立回零仅适用于固定零位站点")
            if event == "capture_home" and self.maintenance.factory_zero:
                raise ValueError("此设备固定使用官方关节零位，无需保存准备位")
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
        if self.task_switching:
            raise ValueError("任务切换中，请稍候")
        if self.status.get("phase") != "hold":
            raise ValueError("请先暂停运动，再进行关节调试（无需切换工作模式）")
        if self.session.arbiter.intervention_pending:
            raise ValueError("DAgger介入尚未交还，请先结束介入再调试")
        if (
            getattr(self.recorder, "recording", False)
            or self.emergency.is_set()
            or self.maintenance.latched
            or self.maintenance.state != "idle"
        ):
            raise ValueError("录制或紧急暂停时不可点动")
        self.jog.request(arm, joint, delta)

    def configure_policy(self, *, fusion: str, rtc_delay_steps: int | None = None):
        if fusion not in ("tda_smooth", "sync_hold", "rtc"):
            raise ValueError("推理动作块模式需为同步推理、TDA 或 RTC")
        if fusion == "rtc" and (
            getattr(self.io, "policy_trajectory_hz", 0) > 0
            or self.hz != 30 or abs(self.session.arbiter.action_dt - 1 / 30) > 1e-6
            or self.worker is None
            or not hasattr(self.worker.client, "set_rtc")
        ):
            raise ValueError("RTC 需关闭 100 Hz 轨迹通道并使用可重载 Thor 通信进程")
        if rtc_delay_steps is not None and (
            fusion != "rtc" or type(rtc_delay_steps) is not int
            or not 1 <= rtc_delay_steps <= 10
        ):
            raise ValueError("RTC 前缀步数只可在RTC模式设置为1–10")
        if self.status.get("phase") != "hold" or getattr(self.recorder, "recording", False):
            raise ValueError("请先暂停模型并结束本集录制")
        self.policy_commands.put_nowait((
            "configure", fusion,
            self.rtc_delay_steps if rtc_delay_steps is None else rtc_delay_steps,
        ))

    def restart_policy(self):
        if self.status.get("phase") != "hold" or getattr(self.recorder, "recording", False):
            raise ValueError("请先暂停模型并结束本集录制")
        if self.worker is None or not hasattr(self.worker.client, "restart"):
            raise ValueError("当前策略不支持独立重载")
        self.policy_commands.put_nowait(("restart",))

    def reload_interaction(self):
        if self.status.get("phase") != "hold" or getattr(self.recorder, "recording", False):
            raise ValueError("请先暂停并结束录制，再重载交互规则")
        if self.maintenance.latched or self.maintenance.state != "idle":
            raise ValueError("请先结束维护并解除紧急暂停锁存")
        from .interaction_rules import load_rules
        candidate = load_rules()  # File IO/compilation outside control tick.
        self.policy_commands.put_nowait(("interaction", candidate))

    def change_policy_source(self, *, url):
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if parsed.scheme not in ("ws", "wss") or not parsed.hostname:
            raise ValueError("来源地址需为 ws:// 或 wss://")
        if self.status.get("phase") != "hold" or getattr(self.recorder, "recording", False):
            raise ValueError("请先暂停并结束录制再切换来源")
        if self.worker is None or not hasattr(self.worker.client, "url"):
            raise ValueError("当前会话未创建模型通信客户端")
        self.policy_commands.put_nowait(("source", url))

    def restart_planner(self):
        if self.status.get("phase") != "hold" or getattr(self.recorder, "recording", False):
            raise ValueError("请先暂停模型并结束本集录制")
        if self.worker is None or self.worker.planner is None:
            raise ValueError("当前会话未启用独立动作规划进程")
        self.policy_commands.put_nowait(("restart_planner",))

    def run(self, *, duration=None, auto_start=False, demo=False):
        period = 1 / self.hz
        start = last = deadline = time.monotonic()
        policy_tick_times = deque(maxlen=64)
        tick, missed = 0, 0
        demo_stage = 0
        last_obs_at = None
        last_record_images = {}
        try:
            place_on_cpus("CONTROL")
            while not self.stopping.is_set():
                now = time.monotonic()
                policy_tick_times.append((tick, now))
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
                if isinstance(self.recorder, RECORDING_SESSIONS) and self.recorder.error:
                    self._recording_failed(q, self.recorder.error)
                try:
                    event, requested_at = self.events.get_nowait()
                except queue.Empty:
                    event, requested_at = None, None
                a = self.session.arbiter
                try:
                    policy_command = self.policy_commands.get_nowait()
                except queue.Empty:
                    policy_command = None
                if policy_command is not None and policy_command[0] == "task_barrier":
                    receipt = policy_command[1]
                    if (a.phase != Phase.HOLD or self.recorder.recording
                            or self.recorder.saving or self.maintenance.state != "idle"
                            or self.maintenance.latched or a.intervention_pending
                            or self.jog.target is not None or not self.jog.queue.empty()
                            or receipt.get("cancelled")):
                        receipt["error"] = "请先暂停、结束介入和维护，并等待录制保存完成"
                    else:
                        self.task_switching = True
                        frozen = a._leader_frozen
                        a._transition(Phase.HOLD, a._hold)
                        a._leader_frozen = frozen
                    receipt["done"].set()
                    policy_command = None
                if policy_command is not None and policy_command[0] == "task_release":
                    self.task_switching = False
                    while not self.events.empty():
                        self.events.get_nowait()
                    while not self.takeovers.empty():
                        self.takeovers.get_nowait()
                    event, requested_at = None, None
                    if policy_command[1]:
                        self.outcome = "unknown"
                        self.intervention_recording = InterventionRecordingGate()
                    if len(policy_command) > 2:
                        policy_command[2].set()
                    policy_command = None
                if self.task_switching:
                    policy_command = None
                if policy_command is not None:
                    if (a.phase != Phase.HOLD or getattr(self.recorder, "recording", False)
                            or (policy_command[0] == "interaction" and
                                (self.maintenance.latched or self.maintenance.state != "idle"))):
                        self.operator_error = "推理设置未应用：设备已离开保持状态"
                    else:
                        frozen = a._leader_frozen
                        a._transition(Phase.HOLD, a._hold if policy_command[0] == "interaction" else q)
                        if frozen is not None:
                            a._leader_frozen = frozen
                        if policy_command[0] == "interaction":
                            candidate = policy_command[1]
                            a.interaction_rules = candidate
                            self.io.interaction_rules = candidate
                            self.handle_buttons.interaction_rules = candidate
                            self.operator_error = None
                        elif policy_command[0] == "configure":
                            old_fusion = a.action_buffer.fusion
                            a.streaming = policy_command[1] != "sync_hold"
                            a.execute_steps = (
                                50 if policy_command[1] == "sync_hold"
                                else a.default_execute_steps
                            )
                            a.action_buffer = (
                                PlannedActionBuffer(
                                    a.action_dt, max_action_age=a.max_action_age,
                                    fusion=policy_command[1],
                                ) if a.external_planner else
                                TdaActionBuffer(a.action_dt)
                                if policy_command[1] == "tda_smooth"
                                else ActionBuffer(a.action_dt, max_action_age=a.max_action_age)
                            )
                            if not a.streaming:
                                a.action_buffer.fusion = "sync_hold"
                            elif policy_command[1] == "rtc" and not a.external_planner:
                                a.action_buffer.fusion = "rtc"
                            a.rtc_timeline = (
                                RtcTimeline(delay_steps=policy_command[2])
                                if policy_command[1] == "rtc" else None
                            )
                            if policy_command[1] == "rtc":
                                self.rtc_delay_steps = policy_command[2]
                            if (old_fusion == "rtc") != (policy_command[1] == "rtc"):
                                self.worker.request_transport_mode(policy_command[1] == "rtc")
                            self.operator_error = None
                        elif policy_command[0] == "restart_planner":
                            self.worker.request_planner_restart()
                            self.operator_error = None
                        elif policy_command[0] == "source":
                            self.session.rewind_replay()
                            self.worker.request_source(policy_command[1])
                            self.operator_error = None
                        else:
                            self.worker.request_restart()
                            self.operator_error = None
                button_event = self.handle_buttons.read(
                    buttons, now=now, mode=a.mode, phase=a.phase
                )
                if button_event in ("record", "discard") and not self.recording_allowed:
                    button_event = None
                if self.recording_error and button_event in (
                    "record",
                    "discard",
                    "success",
                    "failure",
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
                if (
                    auto_start and snapshot is not None and demo_stage == 0
                    and (self.worker is None or self.worker.ready)
                ):
                    event, demo_stage = "start", 1
                if demo and demo_stage == 1 and elapsed > 1:
                    event, demo_stage = "takeover", 2
                elif demo and demo_stage == 2 and elapsed > 2:
                    event, demo_stage = "manual_ready", 3
                elif demo and demo_stage == 3 and elapsed > 3:
                    event, demo_stage = "resume_policy", 4
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
                    and event
                    not in (
                        "stop",
                        "hold",
                        "reset_stop",
                        "home",
                        "home_leader",
                        "gravity",
                        "mode:teleop",
                    )
                ):
                    # An old collection command must not interrupt live teleoperation.
                    event, requested_at = None, None
                if self.task_switching and event not in ("stop", "hold"):
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
                if event in ("home", "home_leader", "capture_home", "gravity") and (
                    a.phase != Phase.HOLD
                    or getattr(self.recorder, "recording", False)
                    or self.maintenance.state != "idle"
                    or self.maintenance.latched
                ):
                    self.operator_error = "设备状态已变化，维护指令未执行；请先暂停并结束录制"
                    event = None
                elif event is not None:
                    self.operator_error = None
                if event in ("stop", "home", "home_leader", "gravity", "capture_home", "reset_stop", "hold"):
                    if isinstance(self.recorder, RECORDING_SESSIONS):
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
                if original_event in ("home", "home_leader", "gravity") and event == "hold":
                    self.session.rewind_replay()
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
                    policy_tick=tick,
                )
                leader_homing = (
                    self.maintenance.state == "homing"
                    and self.maintenance.home_leader_only
                    and not self.maintenance.latched
                )
                maintenance_action = self.maintenance.step(q, leader, now=now, dt=period)
                jog_action = self.jog.step(
                    q,
                    allowed=(
                        event is None
                        and not a.intervention_pending
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
                    **({"leader_homing": True} if leader_homing else {}),
                    **({"gravity": True} if self.maintenance.state == "gravity" else {}),
                )
                if a.rtc_timeline is not None:
                    try:
                        a.rtc_timeline.record_submitted(tick, submitted)
                    except RuntimeError as exc:
                        a.hold(submitted)
                        self.operator_error = str(exc)
                if maintenance_action is None and jog_action is None:
                    self.session.submitted(decision)
                if a.rtc_timeline is not None:
                    if (
                        obs is not None and fresh and self.worker is not None
                        and self.worker.ready and a.phase in (Phase.RESUME, Phase.POLICY)
                    ):
                        observation_tick, mapped_at = min(
                            policy_tick_times,
                            key=lambda item: abs(item[1] - observed_at),
                        )
                        if abs(mapped_at - observed_at) <= period:
                            try:
                                prepared = a.request_rtc(
                                    obs_id, now, observed_at, observation_tick, tick,
                                    self.io.limit_policy_target,
                                )
                            except ValueError:
                                prepared = None  # Too old or missing exact action history.
                            if prepared is not None:
                                token, commitment = prepared
                                if self.worker.submit(token, RtcJob(obs, commitment)):
                                    a._last_request_at = now
                                else:
                                    a.pending = None
                                    a.rtc_timeline.pending = None
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
                if a.phase == Phase.HOLD and event == "handback_hold":
                    transitions.append("handback_locked")
                if a.phase == Phase.RESUME and event == "resume_policy":
                    transitions.append("resume_requested")
                if a.phase == Phase.POLICY and previous_phase != Phase.POLICY:
                    transitions.append("policy_started")
                constraint_mask = np.abs(submitted - decision.selected_action) > 1e-8
                row = {
                    "event_requested_at": requested_at,
                    "event_applied_at": apply_done if transitions else None,
                    "transitions": transitions,
                    "intervention_id": self.intervention_id,
                    "intervention_pending": a.intervention_pending,
                    "tick": tick,
                    "policy_reply": self.session.last_reply,
                    "time": now,
                    "mode": a.mode.value,
                    "phase": decision.phase.value,
                    "event": original_event,
                    "maintenance": self.maintenance.state,
                    "home_group": "leader" if self.maintenance.home_leader_only else "follower",
                    "policy_url": getattr(self.worker.client, "url", "") if self.worker else "",
                    "stop_latched": self.maintenance.latched,
                    "epoch": decision.epoch,
                    "source": decision.source,
                    "is_intervention": decision.intervention,
                    "expert_valid": decision.source == "human" and snapshot is not None,
                    "policy_valid": decision.policy_valid,
                    "policy_fusion": a.action_buffer.fusion,
                    "policy_action": decision.policy_action,
                    "human_action": leader if decision.source == "human" else None,
                    "selected_action": decision.selected_action,
                    "bounded_action": decision.action.copy(),
                    "bounded_at": decision_done,
                    "submitted_action": submitted,
                    "apply_returned_at": apply_done,
                    "policy_selection": decision.policy_selection,
                    "policy_write_trace": getattr(self.io, "take_policy_trace", lambda: None)(),
                    "constraint_mask": constraint_mask,
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
                if isinstance(self.recorder, RECORDING_SESSIONS):
                    # Snapshot the running policy, not the startup YAML. Mode
                    # changes are permitted in HOLD between recorded episodes.
                    if not self.recorder.recording:
                        self.recorder.metadata.update({
                            "rtc": a.rtc_timeline is not None,
                            "policy_fusion": a.action_buffer.fusion,
                            "streaming": a.streaming,
                            "action_dt": a.action_dt,
                            "rtc_delay_steps": (
                                a.rtc_timeline.delay_steps
                                if a.rtc_timeline is not None else None
                            ),
                        })
                    previous_mode = self.recorder.mode
                    self.recorder.set_mode(a.mode.value, self.outcome)
                    if previous_mode != a.mode.value:
                        self.outcome = "unknown"
                    if (
                        original_event == "start"
                        and a.phase != Phase.HOLD
                        # Inference rollout and HIL intervention both record
                        # from motion start; collection remains explicit.
                        and a.mode in (Mode.HIL, Mode.INFERENCE)
                        and self.recording_allowed
                    ):
                        self.recorder.start_episode()
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
                record_row = self.intervention_recording.filter(
                    row, a.mode == Mode.HIL and a.intervention_waiting
                    and a.phase != Phase.FAULT and getattr(self.recorder, "recording", True),
                )
                self.recorder.metadata["omitted_intervention_waits"] = self.intervention_recording.audit()
                if record_row is not None and not self.recorder.submit(record_row, images or last_record_images):
                    if isinstance(self.recorder, RECORDING_SESSIONS):
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
                policy_remaining = (
                    a.rtc_timeline.remaining(tick) if a.rtc_timeline is not None
                    else a.action_buffer.remaining(now) if a.streaming
                    else max(0, len(a._chunk) - a._index) if a._chunk is not None else 0
                )
                self.status = {
                    "episode_elapsed_s": 0
                    if self._record_started is None
                    else now - self._record_started,
                    "stop_latched": self.maintenance.latched,
                    "maintenance": self.maintenance.state,
                    "home_group": "leader" if self.maintenance.home_leader_only else "follower",
                    "policy_url": getattr(self.worker.client, "url", "") if self.worker else "",
                    "maintenance_error": self.maintenance.error,
                    "operator_error": self.operator_error or self.session.notice,
                    "interaction_revision": getattr(a.interaction_rules, "revision", "bundled"),
                    "leader_locked": a._leader_frozen is not None,
                    "intervention_pending": a.intervention_pending,
                    "leader_control": self.io.leader_control_status() if hasattr(self.io, "leader_control_status") else [],
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
                    "policy_fusion": a.action_buffer.fusion,
                    "policy_ready": self.worker.ready if self.worker else False,
                    "planner_ready": (
                        self.worker.planner_alive if self.worker and self.worker.planner
                        else False
                    ),
                    "policy_transport_ready": (
                        self.worker._client_ready if self.worker else False
                    ),
                    "policy_restart_error": self.worker.restart_error if self.worker else None,
                    "policy_tda_drop_max": getattr(a.action_buffer, "drop_max", None),
                    "policy_joint_speed_rad_s": (
                        self.io.policy_joint_speed
                        if getattr(self.io, "policy_trajectory_hz", 0) > 0 else None
                    ),
                    "policy_replan_period_s": a.replan_period,
                    "policy_buffer_remaining": policy_remaining,
                    "policy_buffer_seconds": (
                        policy_remaining * a.action_dt if a.rtc_timeline is not None
                        else a.action_buffer.seconds_to_expiry(now) if a.streaming
                        else policy_remaining * a.action_dt
                    ),
                    "rtc_delay_steps": (
                        a.rtc_timeline.delay_steps if a.rtc_timeline is not None else None
                    ),
                    "rtc_accepted_replies": (
                        a.rtc_timeline.accepted_replies
                        if a.rtc_timeline is not None else None
                    ),
                    "rtc_late_replies": (
                        a.rtc_timeline.late_replies
                        if a.rtc_timeline is not None else None
                    ),
                    "policy_observed_rtt_p95_s": a.observed_policy_rtt_p95,
                    "policy_observation_to_ready_p95_s": (a.observed_observation_to_ready_p95),
                    "policy_latency_budget_s": a.policy_latency_budget,
                    "policy_request_reason": a.last_request_reason,
                    "policy_request_pending": a.pending is not None,
                    "policy_waiting_for_reply": (
                        not a.streaming and a.phase in (Phase.POLICY, Phase.RESUME)
                        and a._chunk is not None and a._index >= len(a._chunk)
                        and a.pending is not None
                    ),
                    "policy_action_index": decision.action_index,
                    "policy_trimmed_steps": a.action_buffer.last_trimmed_steps,
                    "policy_seam_max_rad": a.action_buffer.last_seam_max_rad,
                    "policy_constraint_active": bool(
                        np.any(constraint_mask[[0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12]])
                    ),
                    "policy_trajectory_hz": getattr(self.io, "policy_trajectory_hz", 0),
                    "policy_trajectory_mode": getattr(self.io, "policy_trajectory_mode", "second_order"),
                    "policy_trajectory_active": (
                        getattr(self.io, "_policy_trajectory", None) is not None
                    ),
                    "leader_error_rad": error,
                    "frame_age_s": quality.get("age_s"),
                    "arrival_skew_s": quality.get("arrival_skew_s"),
                    "deadline_misses": missed,
                    "record_queue": getattr(
                        self.recorder, "queue_depth", self.recorder.queue.qsize()
                    ),
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
            if isinstance(self.recorder, RECORDING_SESSIONS) and (
                self.recorder.mode == "collect" or self.status.get("phase") == "fault"
            ):
                self.recorder.stop_episode("aborted")
            self.recorder.metadata["terminal_status"] = dict(self.status)
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
    p.add_argument(
        "--policy-fusion",
        choices=("tda_smooth", "sync_hold", "rtc"),
        help="ordinary policy chunk handling; trained RTC never uses TDA",
    )
    p.add_argument("--action-dt", type=float, help="model action target spacing in seconds")
    p.add_argument(
        "--expected-policy-latency", type=float, help="initial total Thor RPC estimate in seconds"
    )
    p.add_argument("--prefetch-margin", type=float, help="extra deadline reserve in seconds")
    p.add_argument("--web-port", type=int, help="optional local dashboard port")
    p.add_argument("--executor-socket", help="persistent SDK owner socket; session exit holds, never releases")
    p.add_argument(
        "--device-socket",
        default="/tmp/yam-device.sock",
        help="private Unix socket used by the persistent device owner",
    )
    p.add_argument(
        "--device-daemon",
        action="store_true",
        help="own devices persistently and expose only the private Unix socket",
    )
    p.add_argument(
        "--web-only",
        action="store_true",
        help="serve the public Web/API as a proxy without constructing hardware",
    )
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
    if args.device_daemon and args.web_only:
        p.error("--device-daemon and --web-only are mutually exclusive")
    if (args.device_daemon or args.web_only) and not args.device_socket:
        p.error("split services require --device-socket")
    if not np.isfinite(args.segment_seconds) or args.segment_seconds <= 0:
        p.error("segment-seconds must be finite and positive")
    if not np.isfinite(args.min_free_gb) or args.min_free_gb < 0:
        p.error("min-free-gb must be finite and nonnegative")
    if args.web_port is not None and not 1 <= args.web_port <= 65535:
        p.error("web-port must be between 1 and 65535")
    if args.web_host != "127.0.0.1" and args.web_port is None and not args.device_daemon:
        p.error("web-host requires web-port")
    if args.web_host == "0.0.0.0" and not args.web_allowed_host:
        p.error("0.0.0.0 requires at least one explicit --web-allowed-host")
    if not args.check and not args.demo and service is None:
        if args.web_only:
            if args.web_port is None:
                p.error("--web-only requires --web-port")
            from .workbench import serve_web

            return serve_web(args)
        if args.device_daemon:
            from .workbench import serve_device

            return serve_device(args)
        if args.web_port:
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
    hil_cfg = dict(hil_cfg)
    if args.action_dt is not None:
        hil_cfg["action_dt"] = args.action_dt
    action_dt = float(hil_cfg.get("action_dt", 1 / 30))
    if not np.isfinite(action_dt) or action_dt <= 0 or not 1 <= cfg.control_hz <= 100:
        p.error("invalid action_dt/control_hz")
    if not args.mock and args.mode not in ("teleop", "collect") and not args.url:
        p.error("--url is required for local edge inference")
    for key, option in (
        ("policy_fusion", args.policy_fusion),
        ("expected_policy_latency", args.expected_policy_latency),
        ("prefetch_margin", args.prefetch_margin),
    ):
        if option is not None:
            hil_cfg[key] = option
    if args.baseline:
        hil_cfg["policy_fusion"] = "sync_hold"
    if hil_cfg.get("policy_fusion", "tda_smooth") not in ("tda_smooth", "sync_hold", "rtc"):
        p.error("policy_fusion must be tda_smooth, sync_hold or rtc")
    if hil_cfg.get("policy_fusion") == "rtc" and hil_cfg.get("policy_trajectory_hz", 0):
        p.error("RTC requires direct 30 Hz SDK target writes; disable 100 Hz trajectory")
    if hil_cfg.get("policy_fusion") == "rtc" and (
        cfg.control_hz != 30 or abs(action_dt - 1 / 30) > 1e-6
    ):
        p.error("RTC requires the trained 30 Hz policy tick")
    for key, value in hil_cfg.items():
        if key == "policy_fusion":
            continue
        if key == "rtc_delay_steps":
            if type(value) is not int or not 1 <= value <= 10:
                p.error("rtc_delay_steps must be an integer from 1 to 10")
            continue
        if key == "factory_zero_home":
            if not isinstance(value, bool):
                p.error("factory_zero_home must be true or false")
            continue
        if key == "policy_trajectory_hz":
            if not isinstance(value, (float, int)) or not np.isfinite(value) or value < 0:
                p.error("policy_trajectory_hz must be finite and nonnegative")
            continue
        if not isinstance(value, (float, int)) or not np.isfinite(value) or value <= 0:
            p.error(f"invalid hil setting: {key}")
    try:
        if hil_cfg.get("policy_fusion", "tda_smooth") == "tda_smooth":
            TdaActionBuffer(action_dt)
        else:
            ActionBuffer(action_dt)
    except ValueError as exc:
        p.error(str(exc))
    # Check before constructing cameras, motors, sockets or recording threads.
    required = {"numpy", "yaml", "av", "h5py"}
    if args.web_port:
        required.update(("fastapi", "uvicorn", "cv2"))
    if not args.mock:
        required.update(("pyrealsense2", "cv2"))
        if not args.executor_socket:
            required.add("i2rt")
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
                    "action_dt": action_dt,
                    "policy_fusion": hil_cfg.get("policy_fusion", "tda_smooth"),
                    "factory_zero_home": bool(hil_cfg.get("factory_zero_home", False))
                    and not args.mock,
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
    recorder_type = RemoteRecordingSession if service is not None else RecordingSession
    recorder = recorder_type(
        output,
        mode=args.mode,
        segment_seconds=args.segment_seconds,
        min_free_bytes=int(args.min_free_gb * 1024**3),
        fps=cfg.control_hz,
        video_backend=None if service is None else service.video_backend,
        metadata={
            "station": dataclasses.asdict(cfg),
            "mock": args.mock,
            "rtc": hil_cfg.get("policy_fusion") == "rtc",
            "streaming": not args.baseline and hil_cfg.get("policy_fusion") != "sync_hold",
            "action_dt": action_dt,
            "policy_fusion": hil_cfg.get("policy_fusion", "tda_smooth"),
            "expected_policy_latency": hil_cfg.get("expected_policy_latency", 0.2),
            "prefetch_margin": hil_cfg.get("prefetch_margin", 2 / 30),
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
        if not args.mock and not args.executor_socket:
            can_output = prepare_station_can(cfg)
            print(f"CAN ready: {can_output}", flush=True)
            print(
                "Opening four YAM arms: motors may energize and grippers may calibrate. Keep leader buttons released.",
                flush=True,
            )
        if args.executor_socket:
            from .remote_station import RemoteStationIO
            io_type, io_source = RemoteStationIO, args.executor_socket
        else:
            units = build_arm_units(cfg, mock=args.mock)
            io_type, io_source = StationIO, units
        io = io_type(
            io_source,
            mock=args.mock,
            leader_gain=hil_cfg.get("leader_gain", 0.2),
            leader_speed=hil_cfg.get("leader_speed", 0.5),
            policy_trajectory_hz=hil_cfg.get("policy_trajectory_hz", 0),
            policy_joint_speed=hil_cfg.get("policy_trajectory_joint_speed", 3.0),
            policy_joint_acceleration=hil_cfg.get("policy_joint_acceleration", 30.0),
            policy_natural_frequency=hil_cfg.get("policy_natural_frequency", 10.0),
        )
        client = (
            MockPolicy()
            if args.mock
            else ProcessPolicyClient(
                args.url, timeout=hil_cfg.get("request_timeout", 1.5),
                rtc=hil_cfg.get("policy_fusion") == "rtc",
            )
            if args.url
            else None
        )
        policy_worker = (
            PolicyWorker(client, planner=ProcessActionPlanner()) if client else None
        )
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
        # Legacy IO closes SDKs; remote IO only detaches into persistent HOLD
        # unless the operator explicitly requested a supported disconnect.
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
