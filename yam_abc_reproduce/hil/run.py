"""YAM four-mode workstation: uv run --no-sync yam-workstation --mock.

Keyboard: s start/resume, i HIL takeover, space hold, 1/2/3/4 select mode, r collection segment,
q quit (hardware shutdown removes active motor control; support the arms first).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import queue
import sys
import threading
import time
from collections import deque
from contextlib import nullcontext
from importlib.util import find_spec
from pathlib import Path

import numpy as np

from ..camera.worker import CameraWorker
from ..config import (
    RobotConfig,
    build_station_config,
    controller_channel_for,
    load_yaml,
    robot_channel_for,
)
from ..resource_qos import place_on_cpus
from ..robot.can_bus import bring_up_can_buses, check_can_up, stop_can_buses
from ..runtime import build_arm_units, build_cameras_from_config, iter_leader_agents
from .buttons import HandleButtons
from .cleanup import close_resources
from .core import Arbiter, Mode, Phase
from .device_health import MOTION_EVENTS, DeviceHealth
from .human_rtc import SETTINGS as HUMAN_RTC_SETTINGS
from .human_rtc import HumanRtc, HumanRtcConfig
from .jog import Jog
from .maintenance import Maintenance
from .metrics import Latencies
from .observation import Observations
from .planner_process import ProcessActionPlanner
from .pld import PldClient, PldConfig
from .policy import PolicyWorker, RtcJob, reply_record, validate_policy_url
from .policy_process import ProcessPolicyClient
from .recording import RecordingSession
from .recording_service import RemoteRecordingSession
from .rtc_timeline import POLICY_MODES, create_policy_timeline
from .session import Session
from .station import StationIO


class MockPolicy:
    def __init__(self, *, rtc=False):
        self.rtc = rtc

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

HIL_SETTINGS = HUMAN_RTC_SETTINGS | frozenset({
    "action_dt", "action_timeout", "expected_policy_latency", "factory_zero_home",
    "handover_error", "leader_gain", "leader_speed", "max_frame_age", "max_frame_skew",
    "max_state_age", "mirror_error", "pld", "policy_fusion", "policy_joint_acceleration",
    "policy_natural_frequency", "policy_trajectory_hz", "policy_trajectory_joint_speed",
    "prefetch_margin", "replan_period", "request_timeout", "rtc_delay_steps",
    "tick_timeout", "warn_frame_skew",
})


def validate_hil_keys(settings):
    """Reject retired or misspelled settings before constructing any SDK owner."""
    if not isinstance(settings, dict):
        raise ValueError("hil 必须是配置对象")
    unknown = set(settings) - HIL_SETTINGS
    if unknown:
        raise ValueError("未知或已移除的 hil 配置：" + ", ".join(sorted(unknown)))


def station_can_channels(cfg, *, followers_only=False) -> list[str]:
    """Only the CAN interfaces this session actually owns."""
    robots = cfg.robot.robots or [cfg.robot.active_robot()]
    channels = [robot_channel_for(robot) for robot in robots]
    if not followers_only:
        channels += [controller_channel_for(cfg.robot.controller_for(robot)) for robot in robots]
    return list(dict.fromkeys(channels))


def prepare_station_can(cfg, *, followers_only=False) -> str:
    """Bring station CAN up and verify every interface before i2rt opens it."""
    return prepare_can_channels(station_can_channels(cfg, followers_only=followers_only))


def prepare_can_channels(needed) -> str:
    """Shared bring-up for Follower connection and independent Leader activation."""
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
        prompt="pick and place",
        settings=None,
        leader_factory=None,
        leader_prepare=None,
        leader_stop=None,
        leader_config=None,
    ):
        self.io, self.cameras, self.worker, self.recorder = io, cameras, worker, recorder
        # Shared with the existing Leader factory so a later independent enable
        # uses the accepted session setting, not the original startup value.
        self.leader_config = leader_config if leader_config is not None else RobotConfig()
        self.leaders_connected = getattr(io, "leaders_connected", True)
        self.leader_connection = None
        if leader_factory is not None:
            from .leader_connection import LeaderConnection
            options = {}
            if leader_prepare is not None:
                options["prepare"] = leader_prepare
            if leader_stop is not None:
                options["stop"] = leader_stop
            self.leader_connection = LeaderConnection(io, leader_factory, **options)
            io.leader_connection = self.leader_connection
        self.hz, self.prompt = hz, prompt
        settings = {} if settings is None else settings
        validate_hil_keys(settings)
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
                action_dt=action_dt,
                max_request_age=settings.get("request_timeout", 1.5),
                max_action_age=settings.get("action_timeout", 1.5),
                tick_timeout=settings.get("tick_timeout", 0.5),
                handover_error=settings.get("handover_error", 0.2),
                replan_period=settings.get("replan_period", 0.2),
                expected_policy_latency=settings.get("expected_policy_latency", 0.2),
                prefetch_margin=settings.get("prefetch_margin", 2 / 30),
                policy_fusion=settings.get("policy_fusion", "sync_hold"),
                external_planner=worker is not None and worker.planner is not None,
                rtc_delay_steps=self.rtc_delay_steps,
            ),
            worker,
            rtc_limit_target=getattr(io, "limit_policy_target", None),
            human_rtc=HumanRtc(HumanRtcConfig.from_settings(settings), action_dt=action_dt),
        )
        if self.session.arbiter.rtc_timeline is not None and (
            getattr(io, "policy_trajectory_hz", 0) > 0
            or not callable(getattr(io, "limit_policy_target", None))
            or hz != 30 or abs(action_dt - 1 / 30) > 1e-6
        ):
            raise ValueError("RTC requires 30 Hz direct SDK writes and shared hard limits")
        self.events = queue.Queue(maxsize=16)
        self.policy_commands = queue.Queue(maxsize=4)
        self.task_releases = queue.SimpleQueue()
        self.policy_command_status = None
        self.recording_mode = "standard"
        self.task_switching = False
        self.takeovers = queue.Queue(maxsize=1)
        self.intervention_id = 0
        self.stopping = threading.Event()
        self.holding = queue.Queue(maxsize=1)
        self.status = {"phase": "hold", "mode": mode, "tick": 0}
        self.device_health = DeviceHealth(settings.get("max_state_age", .25),
            expected_arms=4 if self.leaders_connected else 2)
        self._health_fault_logged = None
        self.handle_buttons = HandleButtons()
        self.outcome = "unknown"
        self.latencies = Latencies()
        self.emergency = threading.Event()
        self.jog = Jog()
        from .cartesian_jog import CartesianJog
        self.cartesian = CartesianJog()
        self.cartesian_commands = queue.Queue(maxsize=1)
        self.maintenance = Maintenance(
            factory_zero=bool(settings.get("factory_zero_home", False)) and not io.mock,
            leaders_connected=self.leaders_connected,
        )
        self.operator_error = None
        self.recording_error = None
        self._record_started = None
        self.recording_allowed = True
        self._set_pld(PldClient(settings.get("pld")))
        self._submitted_history = deque(maxlen=128)
        self._pld_pending = None

    def _set_pld(self, client):
        """Install on the control owner; in-flight requests retain their own identity."""
        self.pld = self.session.pld = client
        self.io.grasp_diagnostics_enabled = client.enabled or self.recording_mode == "grasp_diagnostics"

    def _apply_plain_policy_selection(self, event):
        # The ordinary-inference card/shortcut is an explicit opt-out, not a
        # fallback when expert validation fails. RL Apply is a separate command.
        if event in ("mode:inference", "end_intervention:inference"):
            self._set_pld(PldClient())

    def rotate_pld_session(self):
        """A task/data-session change starts an independent Thor run, not a grasp."""
        self._set_pld(PldClient(self.pld.config.as_dict()))

    def configure_pld(self, **settings):
        candidate = PldConfig.from_dict(settings)
        a = self.session.arbiter
        if candidate.mode != "off" and a.rtc_timeline is None:
            raise ValueError("PLD-Grasp 需要原生 RTC；请先在推理设置选择 RTC")
        client = PldClient(candidate.as_dict())
        client.prepare(getattr(self.worker.client, "metadata", None) if self.worker else None,
                       rtc=a.rtc_timeline is not None)
        self._queue_policy_command(("pld", client))

    def evaluate_pld_round(self, *, correct, total, episode_id):
        if (type(correct) is not int or type(total) is not int
                or not 0 <= correct <= total or total <= 0):
            raise ValueError("整轮评估需要 0 ≤ C ≤ N 且 N > 0")
        if (self.session.arbiter.phase != Phase.HOLD or self.recorder.recording
                or self.recorder.saving or not episode_id
                or episode_id != getattr(self.recorder, "episode_id", None)):
            raise ValueError("请暂停保存后为当前已结束的一集填写C/N；集号变化时重新核对")
        if not self.recorder.record_policy_reply(dict(
            event="evaluation", episode_id=episode_id, time=time.monotonic(),
            details={"evaluation": dict(C=correct, N=total, purpose="evaluation_only")})):
            raise ValueError("整轮评估记录失败")

    def previous_command(self, observation_tick, observed_at):
        """Exact previous tick, already submitted before the observation anchor."""
        for tick, submitted_at, command in reversed(self._submitted_history):
            if tick == observation_tick - 1 and submitted_at <= observed_at:
                return command.copy(), dict(previous_command_tick=tick,
                                            previous_command_submitted_at=submitted_at)
        return None

    def _audit_pld(self, payload, *, event, session_path=None, now=None, tick=None):
        """Nonblocking append to the existing recorder owner, including late replies."""
        if not getattr(self.recorder, "record_policy_reply", lambda *a, **k: False)(
            dict(event=event, time=time.monotonic() if now is None else now,
                 tick=tick, details={"policy_reply": payload}),
            session_path=session_path):
            raise RuntimeError("PLD-Grasp 完整响应记录失败；已拒绝继续执行")

    def _finish_pld_request(self):
        """After physical HOLD only: retire a bounded in-flight reply, never act on it.

        Stopping the control loop must not lose a reply already in the network
        owner. Timeout records cancellation, not a fabricated model response.
        """
        if self._pld_pending is None:
            return
        timeout = min(4.0, max(.1, getattr(self.worker.client, "timeout", 1.5) + 1.1))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            reply = self.worker.poll()
            if reply is not None:
                record = reply_record(reply, now=time.monotonic(), discarded=True)
                record.update(cancelled=True, error=reply.error or "runtime_closed")
                self._audit_pld(record, event="reply", session_path=reply.recording_session)
                self._pld_pending = None
                return
            time.sleep(.01)
        if not self._pld_pending.get("cancelled"):
            self._audit_pld(dict(self._pld_pending, cancelled=True, discarded=True,
                error="runtime_closed_without_reply"), event="cancelled",
                session_path=self._pld_pending["recording_session"])
        self._pld_pending = None


    def _recording_failed(self, state, leader, reason):
        """Abort the episode and signal HOLD without latching a motor fault."""
        if self.recording_error is not None:
            return
        self.recording_error = str(reason)
        self.outcome = "aborted"
        self.recorder.metadata["recording_error"] = self.recording_error
        self.recorder.abort_episode()
        self.session.arbiter.hold(state)
        self.session.arbiter._leader_frozen = leader.copy()
        hold_errors = self.io.hold()
        if hold_errors:
            raise RuntimeError("hold after recording failure: " + "; ".join(hold_errors))

    def event(self, event):
        if self.task_switching and event not in ("stop", "hold", "quit"):
            raise ValueError("任务切换中，请稍候")
        allowed = {
            "ack_device_health",
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
            "end_intervention:teleop",
            "end_intervention:inference",
            "end_intervention:hil",
            "end_intervention:collect",
        }
        if event not in allowed:
            raise ValueError("unknown event")
        if reason := self._leader_event_error(event):
            raise ValueError(reason)
        if event in MOTION_EVENTS:
            self._require_device_health(event)
        if event == "ack_device_health" and self.status.get("phase") != "hold":
            raise ValueError("请先暂停，再确认设备故障已处理")
        if event.startswith("mode:") and self.session.arbiter.intervention_pending:
            raise ValueError("介入尚未结束，请明确确认结束介入并切换模式")
        if event == "start" and self.session.arbiter.intervention_pending:
            raise ValueError("DAgger介入尚未结束，请选择暂停或交还模型")
        if (event in ("start", "resume_policy") and self.session.policy_error
                and self.session.arbiter.mode in (Mode.INFERENCE, Mode.HIL)):
            raise ValueError("推理已暂停，请先重载推理通信或动作规划；机械臂无需断开")
        if (event in ("start", "resume_policy")
                and self.session.arbiter.mode in (Mode.INFERENCE, Mode.HIL)
                and self.worker is not None and not self.worker.ready):
            raise ValueError("推理通信或规划尚未就绪，请检查并重载对应子进程")
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
                "ack_device_health",
                "mode:teleop",
                "end_intervention:teleop",
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

    def request_jog(self, arm, joint, delta=None, *, target=None):
        self._require_device_health()
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
        self.jog.request(arm, joint, delta, target=target)

    def _follower_home_recovery(self, event=None):
        # Only factory-zero Follower homing may run with a historical tracking
        # latch. It never clears that latch or authorizes policy/teleop/jog.
        return self.maintenance.factory_zero and (event == "home" or (
            self.maintenance.state == "homing" and not self.maintenance.home_leader_only))

    def _motion_health_reason(self, event=None):
        if event == "home" and self._follower_home_recovery(event):
            return self.device_health.feedback_reason()
        return self.device_health.blocking_reason()

    def _require_device_health(self, event=None):
        reason = self._motion_health_reason(event)
        if reason:
            raise ValueError(reason)

    def check_device_health(self):
        return self.device_health.snapshot()

    def configure_hil_input(self, *, input):
        if input not in ("leader", "keyboard"):
            raise ValueError("请选择 Leader 或键盘末端微调")
        a = self.session.arbiter
        if a.mode != Mode.HIL:
            raise ValueError("请先切换到 HIL，再选择输入方式")
        self._queue_policy_command(("hil_input", input))
        if input == "keyboard":
            self.cartesian.prepare()

    def request_cartesian(self, *, arm, axis, delta, epoch, command_id, observed_tick):
        self._require_device_health()
        self.cartesian.validate(arm, axis, delta)
        a = self.session.arbiter
        if (a.mode != Mode.HIL or a.hil_input != "keyboard" or a.phase != Phase.HUMAN
                or epoch != a.epoch or self.task_switching or self.emergency.is_set()
                or self.maintenance.latched or self.maintenance.state != "idle"):
            raise ValueError("请先选择键盘微调并介入；暂停、交还或急停后旧指令无效")
        if not 0 <= self.status.get("tick", 0)-observed_tick <= 30:
            raise ValueError("微调所依据的面板状态已过期，请刷新状态后重试")
        self.cartesian_commands.put_nowait(dict(arm=arm, axis=axis, delta=delta,
            epoch=epoch, command_id=command_id, requested_at=time.monotonic()))

    def _settings_error(self, kind):
        """Same authority at HTTP admission and again at the applying tick.

        Reloads may repair a locked intervention or run during background save;
        changing experiment/input/recording settings requires a settled episode.
        """
        if self.task_switching:
            return "任务切换中，设置未提交；完成后可重试"
        if kind in ("leaders", "leader_gravity") and self.leader_connection and self.leader_connection.busy:
            return "Leader连接操作正在进行，请等待；Follower仍保持"
        a = self.session.arbiter
        if a.phase != Phase.HOLD or getattr(self.recorder, "recording", False):
            return "请先暂停运动并结束本集，再修改设置"
        if self.maintenance.latched or self.emergency.is_set():
            return "紧急暂停已锁存，请先检查现场并解除锁存"
        if self.maintenance.state != "idle":
            return "请先结束当前维护操作，再修改设置"
        if kind not in ("restart", "restart_planner", "interaction"):
            if getattr(self.recorder, "saving", False):
                return "请等待本集保存完成，再修改设置"
            if a.intervention_pending:
                return "请先交还或结束介入，再修改设置"
        return None

    def policy_controls(self):
        """UI consumes the owner's rules, rather than reconstructing them in JS."""
        queued = self.policy_command_status and self.policy_command_status["state"] == "queued"
        result = {}
        for name, kind in (("configure", "configure"), ("reload", "restart")):
            reason = self._settings_error(kind) or ("设置请求正在处理" if queued else None)
            result[name] = {"allowed": reason is None, "reason": reason}
        return result

    def _leader_event_error(self, event):
        """Recheck both HTTP admission and the tick after topology/settings change."""
        if self.leader_connection and self.leader_connection.busy and (
            event in MOTION_EVENTS or (event and event.startswith(("mode:", "end_intervention:")))
        ):
            return "Leader正在独立启停，Follower保持；完成后可继续，无需断开Follower"
        if self.leaders_connected:
            return None
        a = self.session.arbiter
        if event == "start" and a.mode in (Mode.TELEOP, Mode.COLLECT):
            return "请先点击“启用 Leader”；Follower无需断开"
        if event in ("takeover", "manual_ready") and a.mode == Mode.HIL and a.hil_input == "leader":
            return "请先暂停并启用 Leader，或选择键盘微调；Follower无需断开"
        if event == "home_leader":
            return "Leader尚未启用；请先点击“启用 Leader”，Follower无需断开"
        if event == "capture_home":
            return "Leader尚未启用，不能保存完整四臂准备位；请先独立启用Leader"
        return None

    def leader_controls(self):
        reason = self._settings_error("leaders")
        if self.leader_connection is None:
            reason = "当前执行器不支持独立Leader启停"
        enable_reason = reason
        if self.session.arbiter.mode == Mode.INFERENCE:
            enable_reason = "普通推理不需要启用Leader"
        return dict(enable=dict(allowed=enable_reason is None, reason=enable_reason),
                    disable=dict(allowed=reason is None, reason=reason))

    def configure_leaders(self, *, enabled, ready=False):
        if not ready:
            raise ValueError("请确认Leader已支撑、行程清空且有人现场照看；停用会释放Leader力矩")
        control = self.leader_controls()["enable" if enabled else "disable"]
        if not control["allowed"]:
            raise ValueError(control["reason"])
        self._queue_policy_command(("leaders", enabled))

    def leader_settings(self):
        reason = self._settings_error("leader_gravity")
        return dict(gravity_comp_factor=self.leader_config.leader_gravity_comp_factor,
                    allowed=reason is None, reason=reason)

    def configure_leader_gravity(self, *, gravity_comp_factor, ready=False):
        if not ready:
            raise ValueError("请确认Leader已支撑、行程清空且有人照看，再更新重力补偿")
        if gravity_comp_factor is None:
            raise ValueError("请填写Leader重力补偿系数")
        RobotConfig(leader_gravity_comp_factor=gravity_comp_factor)
        if not .5 <= gravity_comp_factor <= 1.5:
            raise ValueError("Leader在线试调范围为0.5至1.5；请勿大幅改变重力补偿")
        self._queue_policy_command(("leader_gravity", float(gravity_comp_factor)))

    def _refresh_leaders(self, *, poll=True):
        if self.leader_connection is None:
            return
        # New devices are adopted only BEFORE io.read(), never mid-tick using
        # the absent-Leader placeholder that was captured before connection.
        completed = (self.leader_connection.poll(cancelled=self.emergency.is_set() or self.stopping.is_set())
                     if poll else False)
        connected = self.io.leaders_connected
        if connected != self.leaders_connected:
            self.leaders_connected = connected
            self.maintenance.leaders_connected = connected
            self.device_health.expected_arms = 4 if connected else 2
            self.device_health.clear_window()
            self.handle_buttons = HandleButtons()
        if completed and self.leader_connection.error:
            self.operator_error = ("Leader独立启停失败，Follower未断开。请检查Leader供电和USB-CAN；"
                "如清理未完成，请支撑后重试停用。原始诊断：" + self.leader_connection.error)

    def _queue_policy_command(self, command):
        reason = self._settings_error(command[0])
        if reason:
            raise ValueError(reason)
        receipt = {"kind": command[0], "state": "queued", "submitted_at": time.monotonic()}
        self.policy_commands.put_nowait(("command", command, receipt))
        self.policy_command_status = receipt

    def _apply_settings_command(self, command, q, leader, receipt):
        """Control-owner-only application; enqueueing never changes live settings."""
        kind = command[0]
        a = self.session.arbiter
        reason = self._settings_error(kind)
        if kind == "hil_input" and a.mode != Mode.HIL:
            reason = "状态已变化，介入方式未切换"
        if kind == "leaders" and command[1] and a.mode == Mode.INFERENCE:
            reason = "模式已变化；普通推理不启用Leader"
        if kind not in {"leaders", "leader_gravity", "recording_mode", "hil_input", "pld", "interaction", "configure",
                        "restart_planner", "source", "restart"}:
            reason = "未知设置请求，未应用"
        if reason:
            self.operator_error = reason
            receipt.update(state="rejected", error=reason)
            return
        if kind == "leaders":
            try:
                self.leader_connection.start(command[1])
                self._refresh_leaders(poll=False)
            except Exception as exc:
                self.operator_error = str(exc)
                receipt.update(state="rejected", error=str(exc))
                return
        elif kind == "leader_gravity":
            try:
                self.io.set_leader_gravity_comp_factor(command[1])
            except Exception as exc:
                self.operator_error = "Leader重力补偿更新失败，仍保持暂停：" + str(exc)
                receipt.update(state="rejected", error=self.operator_error)
                return
            self.leader_config.leader_gravity_comp_factor = command[1]
        elif kind == "recording_mode":
            # Recording settings do not invalidate RTC epochs, targets or plans.
            self.recording_mode = command[1]
            self.io.grasp_diagnostics_enabled = self.pld.enabled or self.recording_mode == "grasp_diagnostics"
        elif kind == "hil_input":
            a.hil_input = command[1]
            a._transition(Phase.HOLD, q)
            a._leader_frozen = leader.copy() if a.hil_input == "keyboard" else None
            self.cartesian.cancel()
        else:
            frozen = a._leader_frozen
            a._transition(Phase.HOLD, a._hold if kind == "interaction" else q)
            if frozen is not None:
                a._leader_frozen = frozen
            if kind == "pld":
                self._set_pld(command[1])
                if a.mode not in (Mode.INFERENCE, Mode.HIL):
                    # RL Apply prepares policy control as well as the selected
                    # experiment mode, but never starts execution or enables Leaders.
                    a.change_mode(Mode.INFERENCE, q)
            elif kind == "interaction":
                a.interaction_rules = self.io.interaction_rules = command[1]
                self.handle_buttons.interaction_rules = command[1]
            elif kind == "configure":
                old_fusion = a.policy_fusion
                a.policy_fusion = command[1]
                a.rtc_timeline = create_policy_timeline(command[1], delay_steps=command[2])
                if command[1] == "rtc":
                    self.rtc_delay_steps = command[2]
                if (old_fusion == "rtc") != (command[1] == "rtc"):
                    self.worker.request_transport_mode(command[1] == "rtc")
                    self.session.begin_policy_recovery()
            elif kind == "restart_planner":
                self.worker.request_planner_restart()
                self.session.begin_policy_recovery()
            elif kind == "source":
                self.session.rewind_replay()
                self.worker.request_source(command[1])
                self.session.begin_policy_recovery()
            elif kind == "restart":
                self.worker.request_restart()
                self.session.begin_policy_recovery()
        self.operator_error = None
        receipt.update(state="accepted", applied_at=time.monotonic())

    def _prepare_policy_recording(self, event, state, leader):
        """Admit the ordered start marker before allowing policy execution.

        This is nonblocking queue admission, not a disk-flush acknowledgement.
        Any later writer failure still follows the existing recording HOLD path.
        """
        a = self.session.arbiter
        if ((self.session.policy_error or (self.worker is not None and not self.worker.ready))
                and event in ("start", "resume_policy")
                and a.mode in (Mode.INFERENCE, Mode.HIL)):
            self.operator_error = "推理故障尚未恢复，请重载对应子进程"
            return False
        starts = event == "start" and a.phase == Phase.HOLD and not a.intervention_pending
        resumes = event == "resume_policy" and a.mode == Mode.HIL and (
            a.phase == Phase.HUMAN or (
                a.phase in (Phase.HOLD, Phase.TAKEOVER)
                and (a.intervention_pending or a._leader_frozen is not None)
            )
        )
        keyboard_resumes = (event == "takeover" and a.mode == Mode.HIL
            and a.hil_input == "keyboard" and a.phase == Phase.HOLD and a.intervention_pending)
        if not (starts or resumes or keyboard_resumes) or a.mode not in (Mode.HIL, Mode.INFERENCE):
            return True
        if self.pld.enabled:
            try:
                self.pld.prepare(getattr(self.worker.client, "metadata", None),
                                 rtc=a.rtc_timeline is not None)
            except (ValueError, TypeError, AttributeError) as exc:
                self.operator_error = str(exc)
                return False
        if not isinstance(self.recorder, RECORDING_SESSIONS):
            return True
        if not self.recording_allowed:
            self.operator_error = "当前会话不可录制，未恢复模型运动"
            return False
        if not self.recorder.recording:
            self.outcome = "unknown"
            self.recorder.set_mode(a.mode.value, self.outcome)
            self.recorder.metadata.update({
                "recording_mode": self.recording_mode,
                "hil_input": a.hil_input if a.mode == Mode.HIL else None,
                "rtc": a.rtc_timeline is not None,
                "policy_fusion": a.policy_fusion,
                "streaming": a.streaming,
                "action_dt": a.action_dt,
                "rtc_delay_steps": a.rtc_timeline.delay_steps if a.rtc_timeline is not None else None,
                "action_limits": getattr(self.io, "policy_limits", lambda: None)(),
                "human_rtc": self.session.human_rtc.snapshot(),
            })
            if self.pld.enabled:
                self.recorder.metadata["policy_events"] = "../policy_events.jsonl"
                self.recorder.metadata["pld"] = dict(schema="pld_sac_block_v2",
                    mode=self.pld.config.mode, run_id=self.pld.run_id, **self.pld.identity)
            else:
                self.recorder.metadata["pld"] = {"mode": "off"}
            self.recorder.start_episode()
        if not self.recorder.recording or self.recorder.error:
            self._recording_failed(state, leader, self.recorder.error or "episode start was not admitted")
            return False
        return True

    def configure_recording(self, *, mode: str):
        if mode not in ("standard", "grasp_diagnostics"):
            raise ValueError("记录模式需为普通记录或抓取诊断")
        self._queue_policy_command(("recording_mode", mode))

    def configure_policy(self, *, fusion: str, rtc_delay_steps: int | None = None):
        if self.pld.enabled and fusion != "rtc":
            raise ValueError("请先将 PLD-Grasp 设为 off，再切换同步推理")
        if fusion not in POLICY_MODES:
            raise ValueError("推理动作块模式需为同步推理或 RTC")
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
        self._queue_policy_command((
            "configure", fusion,
            self.rtc_delay_steps if rtc_delay_steps is None else rtc_delay_steps,
        ))

    def restart_policy(self):
        if self.worker is None or not hasattr(self.worker.client, "restart"):
            raise ValueError("当前策略不支持独立重载")
        self._queue_policy_command(("restart",))

    def reload_interaction(self):
        from .interaction_rules import load_rules
        candidate = load_rules()  # File IO/compilation outside control tick.
        self._queue_policy_command(("interaction", candidate))

    def change_policy_source(self, *, url):
        validate_policy_url(url)
        if self.worker is None or not hasattr(self.worker.client, "url"):
            raise ValueError("当前会话未创建模型通信客户端")
        self._queue_policy_command(("source", url))

    def restart_planner(self):
        if self.worker is None or self.worker.planner is None:
            raise ValueError("当前会话未启用独立动作规划进程")
        self._queue_policy_command(("restart_planner",))

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
                self._refresh_leaders()
                q, leader, buttons, ages = self.io.read()
                read_done = time.monotonic()
                if max(age for age in ages if age is not None) > self.max_state_age:
                    raise RuntimeError("SDK state update stale")
                self.device_health.update(self.io.device_health(), now=read_done,
                                          follower=q, leader=leader)
                incident = self.device_health.active_tracking_fault()
                if incident is not None and incident is not self._health_fault_logged:
                    audit = dict(event="device_health_fault", tick=tick, time=now,
                                 episode_id=(getattr(self.recorder, "episode_id", None)
                                             if getattr(self.recorder, "recording", False) else None),
                                 fault=incident)
                    print(json.dumps(audit, ensure_ascii=False), flush=True)
                    # Record before HOLD closes the episode. This out-of-band log
                    # is not filtered out with intervention waiting/HOLD frames.
                    if isinstance(self.recorder, RECORDING_SESSIONS) and self.recorder.recording:
                        if not self.recorder.record_policy_reply(audit):
                            self._recording_failed(q, leader, "设备跟随告警落盘失败")
                    self._health_fault_logged = incident
                if self.device_health.issues:
                    raise RuntimeError(self.device_health.blocking_reason(now=read_done))
                self.observations.add_state(now, q)
                snapshot = self.observations.snapshot(now, self.prompt)
                observation_done = time.monotonic()
                if isinstance(self.recorder, RECORDING_SESSIONS) and self.recorder.error:
                    self._recording_failed(q, leader, self.recorder.error)
                try:
                    event, requested_at = self.events.get_nowait()
                except queue.Empty:
                    event, requested_at = None, None
                a = self.session.arbiter
                try:
                    policy_command = self.task_releases.get_nowait()
                except queue.Empty:
                    try:
                        policy_command = self.policy_commands.get_nowait()
                    except queue.Empty:
                        policy_command = None
                command_receipt = None
                if policy_command is not None and policy_command[0] == "command":
                    _, policy_command, command_receipt = policy_command
                    if now - command_receipt["submitted_at"] > 2:
                        command_receipt.update(state="rejected", error="设置请求已过期，请重试")
                        policy_command = None
                if policy_command is not None and policy_command[0] == "task_barrier":
                    receipt = policy_command[1]
                    failed_writer = receipt.get("recovering") and bool(
                        self.recording_error or self.recorder.error
                    )
                    if (a.phase != Phase.HOLD or self.recorder.recording
                            or (self.recorder.saving and not failed_writer)
                            or self.maintenance.state != "idle"
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
                if policy_command is not None and policy_command[0] == "recorder_replace":
                    policy_command[1].apply(self)
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
                    if len(policy_command) > 2:
                        policy_command[2].set()
                    policy_command = None
                if self.task_switching:
                    if command_receipt is not None:
                        command_receipt.update(state="rejected", error="任务切换中，设置未应用")
                    policy_command = None
                if policy_command is not None:
                    self._apply_settings_command(policy_command, q, leader, command_receipt)
                button_event = self.handle_buttons.read(
                    buttons, now=now, mode=a.mode, phase=a.phase
                )
                if a.mode == Mode.HIL and a.hil_input == "keyboard":
                    button_event = None
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
                if urgent and a.mode == Mode.HIL and (a.phase in (Phase.POLICY, Phase.RESUME)
                        or (a.hil_input == "keyboard" and a.phase == Phase.HOLD and a.intervention_pending)):
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
                        "end_intervention:teleop",
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
                    and self.leaders_connected
                    and a.hil_input == "leader"
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
                if event == "ack_device_health":
                    try:
                        if a.phase != Phase.HOLD or self.maintenance.state != "idle":
                            raise ValueError("请先暂停并结束维护，再确认设备故障已处理")
                        self.device_health.acknowledge(now=time.monotonic())
                        self.operator_error = "已确认；未自动恢复运动。静止检查不证明关节跟随已恢复。"
                    except ValueError as exc:
                        self.operator_error = str(exc)
                    event = None
                health_reason = self.device_health.blocking_reason()
                if health_reason:
                    if event in MOTION_EVENTS and (reason := self._motion_health_reason(event)):
                        self.operator_error = reason
                        event = None
                    if self.device_health.active_tracking_fault():
                        self.jog.clear()
                        self.cartesian.cancel()
                        if (event != "stop" and not self._follower_home_recovery(event)
                                and (a.phase not in (Phase.HOLD, Phase.FAULT)
                                     or self.maintenance.state != "idle")):
                            self.outcome = "aborted"
                            event = "hold"
                        # HOLD must not silently return the Leaders to manual mode.
                        if a._leader_frozen is None:
                            a._leader_frozen = leader.copy()
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
                if reason := self._leader_event_error(event):
                    self.operator_error = reason
                    event = None
                if event in ("stop", "home", "home_leader", "gravity", "capture_home", "reset_stop", "hold") or (
                    event and event.startswith("end_intervention:")
                ):
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
                if event and event.startswith("mode:") and a.intervention_pending:
                    self.operator_error = "介入状态已变化，模式切换未执行；请明确确认结束介入"
                    event = None
                self.session.check_policy_recovery()
                self._apply_plain_policy_selection(event)
                self.session.human_rtc.refresh(a,
                    leaders_connected=self.leaders_connected, pld=self.pld.enabled,
                    ready=self.worker is not None and self.worker.ready and not self.session.policy_error)
                if not self._prepare_policy_recording(event, q, leader):
                    event = None
                decision = self.session.tick(
                    q,
                    leader,
                    now=now,
                    dt=dt,
                    observation_id=obs_id,
                    observation=obs,
                    observed_at=observed_at,
                    fresh=fresh,
                    leader_ready=True,  # Physical leader offset does not gate policy handback.
                    event=event,
                    policy_tick=tick,
                )
                if self.device_health.active_tracking_fault():
                    if a._leader_frozen is None:
                        a._leader_frozen = leader.copy()
                    decision.leader_manual = False
                    decision.leader_freeze = True
                    decision.leader_hold_target = a._leader_frozen.copy()
                reply = self.session.last_reply
                if reply is not None and ((reply.get("pld_request") or {}).get("mode", "off") != "off"
                                          or reply.get("pld") is not None):
                    self._audit_pld(reply, event="reply", tick=tick, now=now,
                                    session_path=reply.get("recording_session"))
                    self._pld_pending = None
                if (self._pld_pending is not None and not self._pld_pending.get("cancelled")
                        and self._pld_pending["token"]["epoch"] != a.epoch):
                    # The request event is already queued on the recorder. Do not
                    # mutate that snapshot while its writer may be serializing it.
                    self._pld_pending = dict(self._pld_pending, cancelled=True)
                    self._audit_pld(dict(self._pld_pending, discarded=True, error="cancelled_by_epoch"),
                        event="cancelled", tick=tick, now=now,
                        session_path=self._pld_pending["recording_session"])
                keyboard_active = (a.mode == Mode.HIL and a.hil_input == "keyboard"
                    and a.phase == Phase.HUMAN and event is None
                    and not self.emergency.is_set() and not self.maintenance.latched
                    and self.maintenance.state == "idle")
                if not keyboard_active:
                    self.cartesian.cancel()
                try:
                    command = self.cartesian_commands.get_nowait()
                except queue.Empty:
                    command = None
                if command is not None and keyboard_active and command["epoch"] == a.epoch:
                    try:
                        if time.monotonic()-command["requested_at"] > .5:
                            raise ValueError("微调指令已过期，该步未执行")
                        self.cartesian.request(arm=command["arm"], axis=command["axis"],
                            delta=command["delta"], epoch=a.epoch, command_id=command["command_id"],
                            target=a._hold, measured=q, now=time.monotonic())
                    except (ValueError, queue.Full) as exc:
                        self.cartesian.error = str(exc) or "微调计算中，请稍候"
                if keyboard_active:
                    target = self.cartesian.poll(a.epoch, time.monotonic())
                    if target is not None:
                        joints = np.ones(14, dtype=bool)
                        joints[[6, 13]] = False
                        if np.max(np.abs(target[joints]-q[joints])) > self.cartesian.MAX_TRACKING_ERROR_RAD:
                            self.cartesian.applied = None
                            self.cartesian.error = "微调反馈偏差过大，该步未执行；请暂停检查"
                        else:
                            a._hold = target.copy()
                            decision.action = target
                            decision.selected_action = target.copy()
                if (self.session.policy_error and a.mode in (Mode.INFERENCE, Mode.HIL)
                        and isinstance(self.recorder, RECORDING_SESSIONS)):
                    self.recorder.stop_episode("aborted")
                leader_homing = (
                    self.maintenance.state == "homing"
                    and self.maintenance.home_leader_only
                    and not self.maintenance.latched
                )
                was_homing = self.maintenance.state == "homing"
                maintenance_action = self.maintenance.step(q, leader, now=now, dt=period)
                if was_homing and maintenance_action is None and self.maintenance.error:
                    # A blocked/aborted home must stop pushing its previous
                    # trajectory target into the obstacle; HOLD fresh feedback.
                    a.hold(q)
                    a._leader_frozen = leader.copy()
                    decision.action = q.copy()
                    decision.selected_action = q.copy()
                    decision.leader_manual = False
                    decision.leader_freeze = True
                    decision.leader_hold_target = leader.copy()
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
                    mirror=a.mode == Mode.HIL and a.hil_input == "leader",
                    **(
                        {"maintenance_leader": maintenance_action[1]}
                        if maintenance_action is not None
                        else {}
                    ),
                    **({"leader_homing": True} if leader_homing else {}),
                    **({"gravity": True} if self.maintenance.state == "gravity" else {}),
                )
                apply_done = time.monotonic()
                self.device_health.submitted(
                    submitted, getattr(self.io, "last_leader_target", leader), now=apply_done,
                    enabled=[self.maintenance.state != "gravity"] * 2
                    + [getattr(self.io, "leader_position_controlled", False)] * 2,
                )
                self._submitted_history.append((tick, time.monotonic(), submitted.copy()))
                self.session.human_rtc.record_submitted(a, submitted, tick=tick, now=apply_done)
                if a.rtc_timeline is not None:
                    try:
                        a.rtc_timeline.record_submitted(tick, submitted)
                    except RuntimeError as exc:
                        a.hold(submitted)
                        self.operator_error = (
                            "RTC动作一致性异常，已暂停策略：承诺动作与下发目标不同。"
                            "请保持暂停并检查主从姿态及诊断信息，不要连续重试。" + str(exc)
                        )
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
                        previous = self.previous_command(observation_tick, observed_at)
                        if abs(mapped_at - observed_at) <= period and (not self.pld.enabled or previous is not None):
                            try:
                                prepared = a.request_rtc(
                                    obs_id, now, observed_at, observation_tick, tick,
                                    self.io.limit_policy_target,
                                )
                            except ValueError:
                                prepared = None  # Too old or missing exact action history.
                            if prepared is not None:
                                token, commitment = prepared
                                try:
                                    payload = self.pld.build_request(token,
                                        episode_id=getattr(self.recorder, "episode_id", None),
                                        previous_command=None if previous is None else previous[0])
                                    context = dict(previous[1] if previous is not None else {},
                                        observation_reference_time=observed_at,
                                        committed_actions=commitment.actions.copy(),
                                        sync=quality, prefix_sources=a.rtc_timeline.prefix_sources(
                                            observation_tick, commitment.delay_steps))
                                    job = RtcJob(obs, commitment, payload if self.pld.enabled else None,
                                        str(self.recorder.path), context)
                                except ValueError as exc:
                                    self.session.suspend_policy(q, leader, str(exc))
                                    job = None
                                if job is not None and self.worker.submit(token, job):
                                    if self.pld.enabled:
                                        self._pld_pending = dict(token=dataclasses.asdict(token),
                                            pld_request=payload, recording_session=str(self.recorder.path),
                                            request_context=context, cancelled=False)
                                        self._audit_pld(self._pld_pending, event="request", tick=tick, now=now)
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
                bookkeeping_done = time.monotonic()
                transitions = []
                keyboard_takeover = (a.mode == Mode.HIL and a.hil_input == "keyboard"
                    and a.phase == Phase.HUMAN and event == "takeover"
                    and previous_phase in (Phase.POLICY, Phase.RESUME))
                if (a.phase == Phase.TAKEOVER and previous_phase != Phase.TAKEOVER) or keyboard_takeover:
                    self.intervention_id += 1
                    transitions.append("takeover_applied")
                if a.phase == Phase.HUMAN and (previous_phase == Phase.TAKEOVER or keyboard_takeover):
                    transitions.append("human_started")
                if a.phase == Phase.HOLD and event == "handback_hold":
                    transitions.append("handback_locked")
                if a.phase == Phase.RESUME and event == "resume_policy":
                    transitions.append("resume_requested")
                if a.phase == Phase.POLICY and previous_phase != Phase.POLICY:
                    transitions.append("policy_started")
                constraint_mask = np.abs(submitted - decision.selected_action) > 1e-8
                leader_control = self.io.leader_control_status() if hasattr(self.io, "leader_control_status") else []
                row = {
                    "event_requested_at": requested_at,
                    "event_applied_at": apply_done if transitions else None,
                    "transitions": transitions,
                    "intervention_id": self.intervention_id,
                    "device_tracking_fault": self.device_health.active_tracking_fault(),
                    "intervention_pending": a.intervention_pending,
                    "tick": tick,
                    "policy_reply": self.session.last_reply,
                    "handover": (decision.policy_selection or {}).get("handover"),
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
                    "expert_valid": decision.source == "human" and snapshot is not None
                        and (a.hil_input != "keyboard" or a.mode != Mode.HIL or self.cartesian.applied is not None),
                    "policy_valid": decision.policy_valid,
                    "policy_fusion": a.policy_fusion,
                    "policy_action": decision.policy_action,
                    "human_action": (decision.selected_action if a.mode == Mode.HIL
                        and a.hil_input == "keyboard" else leader) if decision.source == "human" else None,
                    "human_input": ({"mode": "keyboard", "command": self.cartesian.snapshot()["applied"]}
                        if a.mode == Mode.HIL and a.hil_input == "keyboard" else {"mode": "leader"}),
                    "selected_action": decision.selected_action,
                    "bounded_action": decision.action.copy(),
                    "bounded_at": decision_done,
                    "submitted_action": submitted,
                    "apply_returned_at": apply_done,
                    "policy_selection": decision.policy_selection,
                    "policy_write_trace": getattr(self.io, "take_policy_trace", lambda: None)(),
                    "constraint_mask": constraint_mask,
                    "measured_state": q,
                    "leader_state": leader if self.leaders_connected else None,
                    "leaders_connected": self.leaders_connected,
                    "leader_connection": self.leader_connection.snapshot() if self.leader_connection else None,
                    "leader_control": leader_control,
                    "leader_gravity_comp_factor": self.leader_config.leader_gravity_comp_factor,
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
                    "sdk_write_timings_s": dict(getattr(self.io, "write_timings_s", {})),
                }
                if self.recording_mode == "grasp_diagnostics" or self.pld.enabled:
                    row["grasp_diagnostics"] = {
                        "schema_version": 1,
                        "sample_phase": "before_command",
                        "followers": getattr(self.io, "gripper_feedback", [None, None]),
                    }
                if isinstance(self.recorder, RECORDING_SESSIONS):
                    # Snapshot the running policy, not the startup YAML. Mode
                    # changes are permitted in HOLD between recorded episodes.
                    if not self.recorder.recording:
                        self.recorder.metadata.update({
                            "leaders_connected": self.leaders_connected,
                            "leader_gravity_comp_factor": self.leader_config.leader_gravity_comp_factor,
                            "recording_mode": self.recording_mode,
                            "rtc": a.rtc_timeline is not None,
                            "policy_fusion": a.policy_fusion,
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
                    if isinstance(self.recorder, RECORDING_SESSIONS):
                        self._recording_failed(q, leader, self.recorder.error or "recorder unavailable")
                    else:
                        raise RuntimeError(self.recorder.error or "recorder unavailable")
                submitted_done = time.monotonic()
                timings = dict(
                    io_read=read_done - now,
                    observation=observation_done - read_done,
                    arbitration=decision_done - observation_done,
                    io_apply=apply_done - decision_done,
                    policy_bookkeeping=bookkeeping_done - apply_done,
                    record_submit=submitted_done - bookkeeping_done,
                    tick_interval=dt,
                    **{
                        f"io_{name}": seconds
                        for name, seconds in getattr(self.io, "read_timings_s", {}).items()
                    },
                    **{
                        f"write_{name}": seconds
                        for name, seconds in getattr(self.io, "write_timings_s", {}).items()
                    },
                )
                if getattr(self.recorder, "recording", False):
                    if self._record_started is None:
                        self._record_started = now
                else:
                    self._record_started = None
                policy_remaining = (
                    a.rtc_timeline.remaining(tick) if a.rtc_timeline is not None
                    else max(0, len(a._chunk) - a._index) if a._chunk is not None else 0
                )
                self.status = {
                    "hil_input": a.hil_input,
                    "cartesian": self.cartesian.snapshot(),
                    "epoch": a.epoch,
                    "episode_id": getattr(self.recorder, "episode_id", None),
                    "action_limits": getattr(self.io, "policy_limits", lambda: None)(),
                    "pld": self.pld.snapshot(
                        getattr(self.worker.client, "metadata", None) if self.worker else None,
                        rtc=a.rtc_timeline is not None),
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
                    "leader_alignment_ready": bool(a._alignment and a._alignment["ready"]
                                                   and error <= a.interaction_rules.ALIGN_TOLERANCE),
                    "leader_alignment_error": a.alignment_error,
                    "leader_alignment_joint_error": a._alignment["error"] if a._alignment else None,
                    "intervention_pending": a.intervention_pending,
                    "leader_control": leader_control,
                    "ready_pose": self.maintenance.ready,
                    "ready_version": self.maintenance.ready_version,
                    "updated_at": time.monotonic(),
                    "device_health": self.device_health.snapshot(),
                    "follower_state": q.tolist(),
                    "leader_state": leader.tolist() if self.leaders_connected else None,
                    "leaders_connected": self.leaders_connected,
                    "leader_connection": self.leader_connection.snapshot() if self.leader_connection else None,
                    "leader_controls": self.leader_controls(),
                    "leader_settings": self.leader_settings(),
                    "sdk_state_age_s": list(ages),
                    "gripper_limits": [
                        getattr(unit.robot, "gripper_limits", lambda: None)()
                        for unit in getattr(self.io, "units", [])
                    ],
                    "buttons": buttons,
                    "jog_active": self.jog.target is not None,
                    "jog_error": self.jog.error,
                    "performance": self.latencies.cached,
                    "record_metrics": getattr(self.recorder, "metrics", {}),
                    "tick": tick,
                    "mode": a.mode.value,
                    "phase": a.phase.value,
                    "source": decision.source,
                    "policy_fusion": a.policy_fusion,
                    "policy_ready": self.worker.ready if self.worker else False,
                    "policy_error": self.session.policy_error,
                    "planner_ready": (
                        self.worker.planner_alive if self.worker and self.worker.planner
                        else False
                    ),
                    "human_rtc": self.session.human_rtc.snapshot(),
                    "policy_transport_ready": (
                        self.worker._client_ready if self.worker else False
                    ),
                    "policy_restart_error": self.worker.restart_error if self.worker else None,
                    "policy_wait_reason": (
                        "正在等待有效模型动作；主从姿态偏差不阻止交还。"
                        if a.phase == Phase.RESUME else None
                    ),
                    "policy_command": dict(self.policy_command_status) if self.policy_command_status else None,
                    "policy_controls": self.policy_controls(),
                    "recording_mode": self.recording_mode,
                    "grasp_feedback_available": [
                        bool(f and f.get("valid")) for f in
                        getattr(self.io, "gripper_feedback", [None, None])
                    ],
                    "policy_joint_speed_rad_s": (
                        self.io.policy_joint_speed
                        if getattr(self.io, "policy_trajectory_hz", 0) > 0 else None
                    ),
                    "policy_replan_period_s": a.replan_period,
                    "policy_buffer_remaining": policy_remaining,
                    "policy_buffer_seconds": policy_remaining * a.action_dt,
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
                    "policy_constraint_active": bool(
                        np.any(constraint_mask[[0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12]])
                    ),
                    "policy_trajectory_hz": getattr(self.io, "policy_trajectory_hz", 0),
                    "policy_trajectory_mode": getattr(self.io, "policy_trajectory_mode", "second_order"),
                    "policy_trajectory_active": (
                        getattr(self.io, "_policy_trajectory", None) is not None
                    ),
                    "leader_error_rad": error if self.leaders_connected else None,
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
                self.status["performance"] = self.latencies.finish_tick(now, **timings)
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
            self.device_health.issues = [f"设备控制检查失败：{type(exc).__name__}: {exc}"]
            self.status = dict(self.status, phase="fault", error=f"{type(exc).__name__}: {exc}",
                               device_health=self.device_health.snapshot())
        finally:
            self.cartesian.close()
            hold_errors = self.io.hold()
            if hold_errors:
                self.status = dict(self.status, hold_errors=hold_errors)
            if isinstance(self.recorder, RECORDING_SESSIONS) and (
                self.recorder.mode == "collect" or self.status.get("phase") == "fault"
            ):
                self.recorder.stop_episode("aborted")
            self.recorder.metadata["terminal_status"] = dict(self.status)
            try:
                self._finish_pld_request()
            except RuntimeError as exc:
                self.status["recording_error"] = str(exc)
            # A hardware fault never implicitly selects manual/gravity mode.
            # HOLD failures remain visible until explicit shutdown/recovery.
            if self.status.get("phase") == "fault" and not self.io.mock:
                print(
                    "Fault latched; position HOLD requested only where feedback is valid. "
                    "Check hold_errors and support arms, then q to shut down.",
                    flush=True,
                )
                while not self.stopping.wait(0.1):
                    pass
            self.status = dict(self.status, running=False)
        return self.status


def validate_station(cfg, *, mock, check_cameras=True, followers_only=False):
    if mock:
        return
    if cfg.robot.num_arm_joints != 6 or {r.type for r in cfg.robot.robots} != {
        "yam_left",
        "yam_right",
    }:
        raise ValueError("station must have two standard six-joint YAM followers")
    if not followers_only and (len(cfg.robot.controllers) != 2 or {c.type for c in cfg.robot.controllers} != {
        "yam_lead_left",
        "yam_lead_right",
    }):
        raise ValueError("use official motorized YAM leaders, not GELLO")
    for r in cfg.robot.robots:
        if r.gripper not in ("linear_4310", "linear_3507"):
            raise ValueError("set the verified parallel gripper motor type in the station YAML")
        if not followers_only and cfg.robot.controller_for(r).controls != r.type:
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
    p.add_argument("--enable-leaders", action="store_true",
                   help="headless only: explicitly connect Leaders at startup; GUI uses independent buttons")
    p.add_argument("--url", help="Thor WebSocket URL on the local Ethernet link")
    p.add_argument("--output", type=Path)
    p.add_argument("--pld-config", type=Path, help="PLD-Grasp mode/identity JSON; default off")
    p.add_argument("--segment-seconds", type=float, default=60, help="采集文件分段时长，不拆逻辑集")
    p.add_argument("--min-free-gb", type=float, default=0.5, help="录制保留空间 GiB")
    p.add_argument("--duration", type=float)
    p.add_argument("--demo", action="store_true", help="mock only: automated takeover/resume")
    p.add_argument(
        "--policy-fusion",
        choices=POLICY_MODES,
        help="full H50 synchronous execution or trained RTC",
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
        help="dashboard listen address; 0.0.0.0 requires an explicit Host policy",
    )
    p.add_argument(
        "--web-allowed-host",
        action="append",
        default=[],
        help="additional HTTP Host name accepted by the dashboard (repeatable)",
    )
    p.add_argument(
        "--web-allow-local-addresses",
        action="store_true",
        help="with 0.0.0.0, accept the actual local IPv4 socket destination as HTTP Host",
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
    if args.web_allow_local_addresses and args.web_host != "0.0.0.0":
        p.error("--web-allow-local-addresses requires --web-host 0.0.0.0")
    if args.web_host == "0.0.0.0" and not (
        args.web_allowed_host or args.web_allow_local_addresses
    ):
        p.error("0.0.0.0 requires --web-allowed-host or --web-allow-local-addresses")
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
    followers_only = not (args.enable_leaders or args.demo or bool(service and service.initializing))
    if args.mode == Mode.INFERENCE and args.executor_socket:
        p.error("当前独立执行器只支持四臂会话；Follower-only普通推理请使用直接SDK连接")
    if args.executor_socket:
        followers_only = False  # Existing v1 executor owns a fixed four-arm roster.
    validate_station(cfg, mock=args.mock, check_cameras=service is None, followers_only=followers_only)
    hil_cfg = load_yaml(args.station).get("hil", {})
    try:
        validate_hil_keys(hil_cfg)
    except ValueError as exc:
        p.error(str(exc))
    hil_cfg = dict(hil_cfg)
    if args.pld_config:
        hil_cfg["pld"] = json.loads(args.pld_config.read_text())
    if service is not None:
        # Interactive connections never restore a previously enabled experiment.
        # Headless CLI --pld-config remains explicit; GUI RL needs operator Apply.
        hil_cfg["pld"] = {"mode": "off"}
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
    if hil_cfg.get("policy_fusion", "sync_hold") not in POLICY_MODES:
        p.error("policy_fusion must be sync_hold or rtc")
    if hil_cfg.get("policy_fusion") == "rtc" and hil_cfg.get("policy_trajectory_hz", 0):
        p.error("RTC requires direct 30 Hz SDK target writes; disable 100 Hz trajectory")
    if hil_cfg.get("policy_fusion") == "rtc" and (
        cfg.control_hz != 30 or abs(action_dt - 1 / 30) > 1e-6
    ):
        p.error("RTC requires the trained 30 Hz policy tick")
    try:
        HumanRtcConfig.from_settings(hil_cfg)
    except (ValueError, TypeError) as exc:
        p.error(str(exc))
    for key, value in hil_cfg.items():
        if key in HUMAN_RTC_SETTINGS:
            continue
        if key == "pld":
            try:
                config = PldConfig.from_dict(value)
                if config.mode != "off" and hil_cfg.get("policy_fusion") != "rtc":
                    p.error("PLD-Grasp requires trained RTC")
            except (ValueError, TypeError) as exc:
                p.error(str(exc))
            continue
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
        create_policy_timeline(hil_cfg.get("policy_fusion", "sync_hold"),
                               delay_steps=hil_cfg.get("rtc_delay_steps", 9))
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
                    "followers_only": followers_only,
                    "action_dt": action_dt,
                    "policy_fusion": hil_cfg.get("policy_fusion", "sync_hold"),
                    "pld_mode": hil_cfg.get("pld", {}).get("mode", "off"),
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
        defer_storage=args.mode == "teleop" or bool(service and service.initializing),
        video_backend=None if service is None else service.video_backend,
        metadata={
            "station": dataclasses.asdict(cfg),
            "mock": args.mock,
            "rtc": hil_cfg.get("policy_fusion") == "rtc",
            "streaming": hil_cfg.get("policy_fusion", "sync_hold") != "sync_hold",
            "action_dt": action_dt,
            "policy_fusion": hil_cfg.get("policy_fusion", "sync_hold"),
            "expected_policy_latency": hil_cfg.get("expected_policy_latency", 0.2),
            "prefetch_margin": hil_cfg.get("prefetch_margin", 2 / 30),
        },
    )
    workers, io, policy_worker, dashboard = [], None, None, None
    runtime = None
    units = []
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
            can_output = prepare_station_can(cfg, followers_only=followers_only)
            print(f"CAN ready: {can_output}", flush=True)
            print(
                "Opening two YAM Followers only; enable Leaders explicitly when needed."
                if followers_only else
                "Opening four YAM arms: motors may energize and grippers may calibrate. Keep leader buttons released.",
                flush=True,
            )
        if args.executor_socket:
            from .remote_station import RemoteStationIO
            io_type, io_source = RemoteStationIO, args.executor_socket
        else:
            units = build_arm_units(cfg, mock=args.mock, followers_only=followers_only)
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
            max_state_age=hil_cfg.get("max_state_age", 0.25),
        )
        client = (
            MockPolicy(rtc=hil_cfg.get("policy_fusion") == "rtc")
            if args.mock
            else ProcessPolicyClient(
                args.url or "", timeout=hil_cfg.get("request_timeout", 1.5),
                rtc=hil_cfg.get("policy_fusion") == "rtc",
            )
        )
        policy_worker = (
            PolicyWorker(client, planner=ProcessActionPlanner(),
                         auto_connect=bool(args.mock or args.url))
        )
        leader_options = {}
        if not args.executor_socket:
            owned_leader_channels = [] if followers_only else [
                channel for channel in station_can_channels(cfg)
                if channel not in station_can_channels(cfg, followers_only=True)]

            def prepare_leaders():
                validate_station(cfg, mock=args.mock, check_cameras=False)
                if not args.mock:
                    owned_leader_channels[:] = [
                        channel for channel in station_can_channels(cfg)
                        if channel not in station_can_channels(cfg, followers_only=True)]
                    prepare_can_channels(owned_leader_channels)

            def stop_leaders():
                if not args.mock:
                    errors = stop_can_buses(owned_leader_channels)
                    if errors:
                        raise RuntimeError("；".join(errors))
                owned_leader_channels.clear()

            leader_options = dict(
                leader_factory=lambda: iter_leader_agents(cfg, io.units, mock=args.mock),
                leader_prepare=prepare_leaders, leader_stop=stop_leaders)
        runtime = Runtime(
            io,
            workers,
            policy_worker,
            recorder,
            mode=args.mode,
            hz=cfg.control_hz,
            action_dt=action_dt,
            prompt=cfg.task_name,
            settings=hil_cfg,
            leader_config=cfg.robot,
            **leader_options,
        )
        if service is not None:
            service.attach(runtime, output)
        if args.web_port:
            from .web import start_dashboard

            dashboard = start_dashboard(runtime, args.web_port)
        from .keyboard import Keyboard

        print(
            "s=start, i=takeover, HIL right handle 1=manual/lock, space=hold, 1/2/3/4=mode, r=record segment, q=shutdown (support arms first)",
            flush=True,
        )
        with Keyboard(runtime.event) if service is None else nullcontext():
            result = runtime.run(duration=args.duration, auto_start=args.demo, demo=args.demo)
        print(json.dumps(result), flush=True)
        recorder = runtime.recorder  # Recovery may have replaced only the data owner.
        if result.get("phase") == "fault":
            raise RuntimeError(result.get("error") or "hardware session aborted")
    finally:
        original_error = sys.exception()
        if runtime is not None:
            recorder = runtime.recorder
        if dashboard:
            dashboard.should_exit = True
        # Hardware releases first; a long recording drain must not delay it.
        # Remote IO detaches to HOLD unless explicitly asked to release.
        devices = []
        if io:
            devices.append(("arms", io.close))
        else:
            for unit in units:
                for role, device in (("leader", unit.agent), ("follower", unit.robot)):
                    close = getattr(device, "close_hil", None)
                    if close:
                        devices.append((f"{unit.name} {role}", close))
        hardware_errors = close_resources(devices)
        if hardware_errors and service is not None:
            service.cleanup_error = "; ".join(hardware_errors)
        resources = []
        if service is None:
            for worker in workers:
                resources.append(("camera", worker.stop))
        for camera in cameras[len(workers) :]:
            resources.append(("camera", camera.stop))
        if policy_worker:
            resources.append(("policy", policy_worker.close))
        cleanup_errors = hardware_errors + close_resources(resources)
        if cleanup_errors:
            recorder.metadata["close_errors"] = cleanup_errors.copy()
        outcome = "aborted" if original_error or cleanup_errors or runtime is None else runtime.outcome
        # A dead transport thread may still have a live child/pipe to reap.
        cleanup_errors += close_resources([("recording", lambda: recorder.close(outcome))])
        if runtime is not None and recorder.error and runtime.recording_error is None:
            runtime.recording_error = recorder.error
        if service is not None:
            service.recording_error = recorder.error
        if not args.mock and not args.executor_socket:
            # Remote sessions never own CAN. Only the persistent executor may
            # release its buses; detaching a session must preserve SDK HOLD.
            only_followers = followers_only or bool(runtime and runtime.leader_connection is not None)
            can_errors = close_resources([("CAN", lambda: stop_can_buses(
                station_can_channels(cfg, followers_only=only_followers)))])
            if can_errors:
                message = "CAN shutdown failed: " + "; ".join(can_errors)
                recorder.metadata.setdefault("close_errors", []).append(message)
                if service is not None:
                    service.cleanup_error = message
                cleanup_errors += can_errors
        if cleanup_errors:
            message = "Session cleanup: " + "; ".join(cleanup_errors)
            print(message, flush=True)
            if original_error is None:
                raise RuntimeError(message)

    if service is not None:
        service.saved()


if __name__ == "__main__":
    main()
