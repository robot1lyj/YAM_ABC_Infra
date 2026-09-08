"""YAM four-mode workstation: python -m yam_abc_reproduce.hil.run --mock.

Keyboard: s start/resume, i toggle HIL takeover, space hold, 1/2/3/4 select mode, r collection segment,
q quit (hardware shutdown removes active motor control; support the arms first).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import queue
import threading
import time
from pathlib import Path

import numpy as np

from ..camera.worker import CameraWorker
from ..config import build_station_config, load_yaml
from ..runtime import build_arm_units, build_cameras_from_config
from .buttons import HandleButtons
from .core import Arbiter, Mode, Phase
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
                replan_period=settings.get("replan_period", 0.2),
            ),
            worker,
        )
        self.events = queue.Queue(maxsize=16)
        self.stopping = threading.Event()
        self.holding = threading.Event()
        self.status = {"phase": "hold", "mode": mode, "tick": 0}
        self.handle_buttons = HandleButtons()
        self.outcome = "unknown"

    def event(self, event):
        allowed = {
            "start",
            "toggle",
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
        if event in ("mode:inference", "mode:hil") and self.worker is None:
            raise ValueError("restart with --url to enable local policy inference")
        if event == "record" and self.status.get("mode") != "collect":
            raise ValueError("record control requires collection mode")
        if (
            event == "record"
            and not getattr(self.recorder, "recording", False)
            and self.status.get("phase") != "human"
        ):
            raise ValueError("start leader teleoperation before recording")
        if event in ("success", "failure"):
            if self.status.get("phase") == "fault":
                raise ValueError("faulted recording remains aborted")
            self.outcome = event
        elif event == "quit":
            self.stopping.set()
        elif event == "hold":
            self.holding.set()
        else:
            self.events.put_nowait(event)

    def run(self, *, duration=None, auto_start=False, demo=False):
        period = 1 / self.hz
        start = last = deadline = time.monotonic()
        tick, missed = 0, 0
        demo_stage = 0
        last_obs_at = None
        try:
            while not self.stopping.is_set():
                now = time.monotonic()
                elapsed = now - start
                if duration is not None and elapsed >= duration:
                    break
                dt = max(period, now - last)
                last = now
                q, leader, buttons, ages = self.io.read()
                if max(ages) > self.max_state_age:
                    raise RuntimeError("SDK state update stale")
                self.observations.add_state(now, q)
                snapshot = self.observations.snapshot(now, self.prompt)
                try:
                    event = self.events.get_nowait()
                except queue.Empty:
                    event = None
                a = self.session.arbiter
                button_event = self.handle_buttons.read(
                    buttons, now=now, mode=a.mode, phase=a.phase
                )
                hold_requested = self.holding.is_set() or button_event == "hold"
                self.holding.clear()
                if button_event:
                    event = button_event
                if auto_start and snapshot is not None and demo_stage == 0:
                    event, demo_stage = "start", 1
                if demo and demo_stage == 1 and elapsed > 1:
                    event, demo_stage = "toggle", 2
                elif demo and demo_stage == 2 and elapsed > 2:
                    event, demo_stage = "toggle", 3
                if hold_requested:
                    while not self.events.empty():
                        self.events.get_nowait()
                    event = "hold"
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
                if a.mode == Mode.HIL and a.phase == Phase.POLICY and error > self.mirror_error:
                    event = "hold"
                recording_event = None
                if a.mode == Mode.COLLECT and event == "toggle":
                    event = "record"
                if event == "record":
                    recording_event, event = event, None
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
                # Never enlarge a motion step because this loop missed its deadline.
                limits = np.full(14, a.max_joint_speed * period)
                limits[[6, 13]] = a.max_gripper_speed * period
                decision.action = np.clip(decision.action, q - limits, q + limits)
                submitted, stamps = self.io.apply(
                    decision, q, leader, dt=period, mirror=a.mode == Mode.HIL
                )
                row = {
                    "tick": tick,
                    "policy_reply": self.session.last_reply,
                    "time": now,
                    "mode": a.mode.value,
                    "phase": decision.phase.value,
                    "event": event,
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
                    previous_mode = self.recorder.mode
                    self.recorder.set_mode(a.mode.value, self.outcome)
                    if previous_mode != a.mode.value:
                        self.outcome = "unknown"
                    if a.mode == Mode.COLLECT:
                        if event == "hold" or a.phase == Phase.FAULT:
                            self.recorder.stop_episode("aborted")
                        elif recording_event:
                            if self.recorder.recording:
                                self.recorder.stop_episode(self.outcome)
                            elif a.phase == Phase.HUMAN and snapshot is not None:
                                self.outcome = "unknown"
                                self.recorder.start_episode()
                        row["record_event"] = recording_event
                if not self.recorder.submit(row, images):
                    raise RuntimeError(self.recorder.error or "recorder unavailable")
                self.status = {
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
                    "recording": getattr(self.recorder, "recording", True),
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
            if self.outcome == "aborted" and not self.io.mock:
                print(
                    "Fault latched; arms held where possible. Support arms, then q to shut down.",
                    flush=True,
                )
                while not self.stopping.wait(0.1):
                    pass
            self.status = dict(self.status, running=False)
        return self.status


def validate_station(cfg, *, mock):
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
    if len(cfg.cameras) != 3 or {c.role for c in cfg.cameras} != {"top", "left", "right"}:
        raise ValueError("configure three D405 camera roles")
    serials = [c.serial for c in cfg.cameras]
    if any(not s or s.startswith("REPLACE") for s in serials) or len(set(serials)) != 3:
        raise ValueError("set three distinct actual D405 serials")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--station", default="configs/station_hil.yaml")
    p.add_argument("--mock", action="store_true")
    p.add_argument("--mode", choices=[m.value for m in Mode], default="hil")
    p.add_argument("--url", help="Thor WebSocket URL on the local Ethernet link")
    p.add_argument("--output", type=Path)
    p.add_argument("--duration", type=float)
    p.add_argument("--demo", action="store_true", help="mock only: automated takeover/resume")
    p.add_argument("--baseline", action="store_true", help="ordinary non-prefetch baseline")
    p.add_argument("--web-port", type=int, help="optional local dashboard port")
    args = p.parse_args()
    if args.demo and not args.mock:
        p.error("--demo is mock only")
    if args.duration is not None and (not np.isfinite(args.duration) or args.duration <= 0):
        p.error("duration must be finite and positive")
    cfg = build_station_config(args.station)
    validate_station(cfg, mock=args.mock)
    hil_cfg = load_yaml(args.station).get("hil", {})
    action_dt = float(hil_cfg.get("action_dt", 1 / 30))
    if not np.isfinite(action_dt) or action_dt <= 0 or not 1 <= cfg.control_hz <= 100:
        p.error("invalid action_dt/control_hz")
    if not args.mock and args.mode not in ("teleop", "collect") and not args.url:
        p.error("--url is required for local edge inference")
    for key, value in hil_cfg.items():
        if not isinstance(value, (float, int)) or not np.isfinite(value) or value <= 0:
            p.error(f"invalid hil setting: {key}")
    output = args.output or Path(cfg.save_root) / time.strftime("hil_%Y%m%d_%H%M%S")
    recorder = RecordingSession(
        output,
        mode=args.mode,
        fps=cfg.control_hz,
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
    cameras = []
    try:
        # Cameras first; no motor constructor until capture has initialized.
        cameras = build_cameras_from_config(cfg, mock=args.mock)
        for camera in cameras:
            worker = CameraWorker(camera)
            workers.append(worker)
            worker.start()
        if not args.mock:
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
        if args.web_port:
            from .web import start_dashboard

            dashboard = start_dashboard(runtime, args.web_port)
        from .keyboard import Keyboard

        print(
            "s=start, i=takeover/resume, space=hold, 1/2/3/4=mode, r=record segment, q=shutdown (support arms first)",
            flush=True,
        )
        with Keyboard(runtime.event):
            result = runtime.run(duration=args.duration, auto_start=args.demo, demo=args.demo)
        print(json.dumps(result), flush=True)
        recorder.close(runtime.outcome)
        if runtime.outcome == "aborted" or recorder.error:
            raise RuntimeError(result.get("error") or recorder.error or "session aborted")
    finally:
        if dashboard:
            dashboard.should_exit = True
        if io:
            io.close()
        else:
            for unit in units:
                for device in (unit.agent, unit.robot):
                    close = getattr(device, "close_hil", None)
                    if close:
                        close()
        for worker in workers:
            worker.stop()
        for camera in cameras[len(workers) :]:
            camera.stop()
        if policy_worker:
            policy_worker.close()
        if recorder._thread.is_alive():
            recorder.close("aborted")


if __name__ == "__main__":
    main()
