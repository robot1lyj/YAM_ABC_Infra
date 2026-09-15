"""Local operator service. Opening the UI never constructs hardware.

One worker owns connect/run/close/export; HTTP handlers enqueue commands only.
Preview JPEG encoding and disk queries live outside the control loop.
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from shutil import disk_usage
from urllib.parse import urlparse

from .camera_slots import CameraSlot
from .core import Mode
from .tasks import Tasks


class Workbench:
    def __init__(self, args):
        self.args = args
        self.mode = args.mode
        self.preview_enabled = True
        self._encoder = None
        self._preview_at = 0.0
        self._saved_version = 0
        self._preview_runtime = None
        self._profile_path = Path("data/workstation") / (
            "ready_mock.json" if args.mock else "ready_real.json"
        )
        self.runtime = None
        self.output = None
        self.state = "disconnected"
        self.error = None
        self.recording_error = None
        self.cleanup_error = None
        self.thread = None
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self._heartbeat = 0.0
        self._operator_lost = False
        self._snapshot = {}
        self._previews = {}
        self._camera_previous = {}
        self._log = deque(maxlen=40)
        self.tasks = Tasks(getattr(args, "task_root", "data/tasks"))
        self.selected_task = None
        self.camera_slots = [CameraSlot(role) for role in ("top", "left", "right")]
        self.camera_state = "disconnected"
        self.camera_error = None
        self.video_backend = None
        self.camera_thread = None
        self._camera_workers = []
        self._camera_generation = 0
        self.task = None
        self.initializing = False
        self.taskless_teleop = False
        self._session_task = None
        self._initialization = {"preflight": None, "accepted": None}
        self._initialization_path = Path("data/workstation") / (
            "initialization_mock.json" if args.mock else "initialization_real.json"
        )
        self._load_initialization()
        self.log("工作台已就绪，设备尚未连接")
        self._monitor = threading.Thread(target=self._observe, daemon=True, name="ui-preview")
        self._monitor.start()
        self._watcher = threading.Thread(
            target=self._watchdog, daemon=True, name="operator-watchdog"
        )
        self._watcher.start()

    def log(self, message):
        self._log.append({"time": time.strftime("%H:%M:%S"), "message": message})

    def _station_path(self):
        return Path(getattr(self.args, "station", "configs/station_hil.yaml"))

    def _station_inventory(self):
        from ..config import (
            build_station_config,
            controller_channel_for,
            robot_channel_for,
        )

        path = self._station_path()
        cfg = build_station_config(path)
        followers = [
            {
                "side": "left" if robot.type.endswith("left") else "right",
                "type": robot.type,
                "channel": robot_channel_for(robot),
                "gripper": robot.gripper,
                "gripper_limits": robot.gripper_limits,
            }
            for robot in cfg.robot.robots
        ]
        leaders = [
            {
                "side": "left" if controller.type.endswith("left") else "right",
                "type": controller.type,
                "channel": controller_channel_for(controller),
                "controls": controller.controls,
                "gripper": cfg.robot.leader_gripper_type,
            }
            for controller in cfg.robot.controllers
        ]
        cameras = [
            {"role": camera.role, "type": camera.type, "serial": camera.serial}
            for camera in cfg.cameras
        ]
        return {
            "station": str(path),
            "station_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "followers": followers,
            "leaders": leaders,
            "cameras": cameras,
        }

    def _load_initialization(self):
        try:
            report = json.loads(self._initialization_path.read_text())
            if report.get("station_sha256") == self._station_inventory()["station_sha256"]:
                self._initialization["accepted"] = report
        except (OSError, ValueError, KeyError):
            pass

    def initialization_preflight(self):
        """Read-only station/config inventory. Never constructs cameras or motors."""
        from ..config import build_station_config
        from .run import validate_station

        inventory = self._station_inventory()
        errors = []
        try:
            cfg = build_station_config(self._station_path())
            validate_station(cfg, mock=self.args.mock)
        except (OSError, ValueError) as exc:
            errors.append(str(exc))

        interfaces = {name for _, name in socket.if_nameindex()}
        can = []
        for item in [*inventory["followers"], *inventory["leaders"]]:
            channel = item["channel"]
            present = bool(self.args.mock or channel in interfaces)
            can.append({"channel": channel, "present": present})
            if not present:
                errors.append(f"未找到 CAN 接口 {channel}")

        expected = {c["serial"] for c in inventory["cameras"] if c.get("serial")}
        detected = set(expected) if self.args.mock else set()
        if not self.args.mock:
            try:
                import pyrealsense2 as rs

                detected = {
                    device.get_info(rs.camera_info.serial_number)
                    for device in rs.context().query_devices()
                }
            except Exception as exc:  # SDK availability is itself a preflight result.
                errors.append("RealSense 枚举失败：" + str(exc))
        missing = sorted(expected - detected)
        if missing:
            errors.append("未找到相机序列号：" + ", ".join(missing))

        result = {
            "ok": not errors,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "can": can,
            "camera_serials": sorted(detected),
            "errors": errors,
        }
        self._initialization["preflight"] = result
        self.log("设备初始化预检通过" if result["ok"] else "设备初始化预检发现问题")
        return result

    def complete_initialization(self, *, gravity_checked, leader_checked):
        if not gravity_checked or not leader_checked:
            raise ValueError("请完成重力补偿和两台 Leader 手感确认")
        if self.state != "connected" or self.runtime is None:
            raise ValueError("请先完成四臂初始化连接")
        ages = self.runtime.status.get("sdk_state_age_s", [])
        if len(ages) != 4 or any(age is None or age >= 0.25 for age in ages):
            raise ValueError("四臂反馈尚未全部稳定，请先检查设备状态")
        cameras = self._snapshot.get("cameras", [])
        if self.camera_state != "connected" or len(cameras) != 3 or not all(
            camera.get("healthy") for camera in cameras
        ):
            raise ValueError("三路相机尚未全部稳定")
        if self.runtime.status.get("maintenance") != "idle":
            raise ValueError("请先结束重力补偿并保持")

        inventory = self._station_inventory()
        measured = []
        for unit in self.runtime.io.units:
            limits = getattr(unit.robot, "gripper_limits", lambda: None)()
            measured.append({"side": unit.name, "gripper_limits": limits})
        report = {
            **inventory,
            "accepted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "gravity_checked": True,
            "leader_checked": True,
            "camera_roles": [camera["role"] for camera in cameras],
            "gripper_measurements": measured,
        }
        self._initialization_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._initialization_path.with_suffix(".tmp")
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        temp.replace(self._initialization_path)
        self._initialization["accepted"] = report
        self.log("设备初始化验收已保存，可用于迁移复核")
        return report

    @property
    def status(self):
        runtime = self.runtime
        live = dict(runtime.status) if runtime else {}
        updated = live.get("updated_at")
        age = None if updated is None else max(0, time.monotonic() - updated)
        health = dict(self._snapshot)
        sampled = health.get("health_sample_at", 0)
        if time.monotonic() - sampled > 1:
            health["cameras"] = [
                {**c, "healthy": False, "fps": 0} for c in health.get("cameras", [])
            ]
        return {
            **health,
            **live,
            "connection": self.state,
            "camera_connection": self.camera_state,
            "camera_error": self.camera_error,
            "video_backend": self.video_backend,
            "tasks": [dict(t) for t in self.tasks.items],
            "selected_task": None if self.selected_task is None else dict(self.selected_task),
            "task_error": self.tasks.error,
            "mock": self.args.mock,
            "mode": live.get("mode", self.mode),
            "connection_error": self.error,
            "recording_error": live.get("recording_error") or self.recording_error,
            "cleanup_error": self.cleanup_error,
            "control_age_s": age,
            "operator_lost": self._operator_lost,
            "policy_configured": bool(self.args.mock or self.args.url),
            "output": None if self.output is None else str(self.output),
            "events": list(self._log),
            "home_available": bool(live.get("ready_pose")),
            "preview_enabled": self.preview_enabled,
            "preview_age_s": max(0, time.monotonic() - self._preview_at)
            if self._preview_at
            else None,
            "home_reason": "请先示教并保存四台机械臂的准备位",
            "initializing": self.initializing,
            "taskless_teleop": self.taskless_teleop,
            "initialization": {**self._initialization, "inventory": self._station_inventory()},
        }

    def heartbeat(self):
        self._heartbeat = time.monotonic()
        self._operator_lost = False

    def _task_editable(self):
        if self.thread and self.thread.is_alive():
            raise ValueError("请先断开机械臂并完成当前会话保存，再切换任务；相机可保持连接")

    def create_task(self, name, instruction, task):
        with self._lock:
            self._task_editable()
            self.selected_task = self.tasks.create(name, instruction, task)
            self.log("已创建并选择任务：" + self.selected_task["name"])
            return dict(self.selected_task)

    def update_task(self, task_id, name, instruction, task):
        with self._lock:
            self._task_editable()
            self.selected_task = self.tasks.update(task_id, name, instruction, task)
            self.log("已更新并选择任务：" + self.selected_task["name"])
            return dict(self.selected_task)

    def select_task(self, task_id):
        with self._lock:
            self._task_editable()
            self.selected_task = self.tasks.get(task_id)
            self.log("已选择任务：" + self.selected_task["name"])
            return dict(self.selected_task)

    def connect_cameras(self):
        with self._lock:
            if self.camera_state in ("connected", "connecting", "disconnecting"):
                raise ValueError("相机已连接或正在切换状态")
            self.camera_state, self.camera_error = "connecting", None
            self.log("正在独立连接三路相机，不启动机械臂")
            self.camera_thread = threading.Thread(
                target=self._open_cameras, daemon=True, name="camera-owner"
            )
            self.camera_thread.start()

    def _open_cameras(self):
        from ..camera.worker import CameraWorker
        from ..config import build_station_config
        from ..runtime import build_cameras_from_config

        drivers, workers = [], []
        try:
            cfg = build_station_config(self.args.station)
            if len(cfg.cameras) != 3 or {c.role for c in cfg.cameras} != {"top", "left", "right"}:
                raise ValueError("请配置top/left/right三路相机")
            serials = [c.serial for c in cfg.cameras]
            if not self.args.mock and (
                any(not s or s.startswith("REPLACE") for s in serials) or len(set(serials)) != 3
            ):
                raise ValueError("请填写三台D405的真实且不同的序列号")
            drivers = build_cameras_from_config(cfg, mock=self.args.mock)
            for driver in drivers:
                worker = CameraWorker(driver)
                workers.append(worker)
                worker.start()
            # Resolve and probe the encoder before a recording episode exists. RKMPP
            # cold-start can take longer than the bounded recording queues allow.
            if self.args.mock:
                self.video_backend = "libx264"
            else:
                from .video import select_backend

                first = workers[0].read()
                if first is None or "rgb" not in first.images:
                    raise RuntimeError("相机首帧不可用于录制编码器预热")
                self.video_backend = select_backend(first.images["rgb"], int(cfg.control_hz))
            if self._closing.is_set():
                raise RuntimeError("工作台正在关闭")
            for slot in self.camera_slots:
                slot.worker = next(w for w in workers if w.role == slot.role)
            self._camera_workers = workers
            self._camera_generation += 1
            self.camera_state = "connected"
            self.log(f"三路相机已连接，录制编码器 {self.video_backend} 已就绪")
        except Exception as exc:
            self.camera_error = str(exc)
            self.video_backend = None
            for device in [*workers, *drivers[len(workers) :]]:
                try:
                    device.stop()
                except Exception as cleanup:
                    self.camera_error += "; 关闭失败：" + str(cleanup)
            self.camera_state = "fault"
            self.log("相机连接失败：" + self.camera_error)

    def disconnect_cameras(self):
        with self._lock:
            if self.camera_state != "connected":
                raise ValueError("相机尚未连接或正在切换状态")
            runtime = self.runtime
            if self.state in ("connecting", "disconnecting") or (
                runtime
                and (
                    runtime.status.get("phase") != "hold"
                    or runtime.recorder.recording
                    or runtime.maintenance.state != "idle"
                )
            ):
                raise ValueError("请先结束录制并暂停机械臂，再断开相机")
            if runtime:
                runtime.event("hold")
            self.camera_state = "disconnecting"
            for slot in self.camera_slots:
                slot.worker = None
            self._previews = {}
            self.camera_thread = threading.Thread(
                target=self._close_cameras, daemon=True, name="camera-close"
            )
            self.camera_thread.start()

    def _close_cameras(self):
        errors = []
        for worker in self._camera_workers:
            try:
                worker.stop()
            except Exception as exc:
                errors.append(str(exc))
        self._camera_workers = []
        self.video_backend = None
        self.camera_error = "; ".join(errors) or None
        self.camera_state = "fault" if errors else "disconnected"
        self.log("相机已断开" if not errors else "相机关闭失败：" + self.camera_error)

    def connect(self, *, ready=False, initialize=False, url=None):
        with self._lock:
            if self.thread and self.thread.is_alive():
                raise ValueError("设备正在连接、运行或整理数据，请等待")
            if (
                not initialize
                and self.selected_task is not None
                and not self.selected_task.get("task")
            ):
                raise ValueError("请编辑当前任务，补填英文 task 后再连接机械臂")
            if self.cleanup_error:
                raise ValueError("上次关闭设备失败，请现场检查并重启工作台")
            if not self.args.mock and ready is not True:
                raise ValueError("请确认机械臂已固定、行程清空并有操作员在场")
            if url is not None:
                parsed = urlparse(url)
                if url and (parsed.scheme not in ("ws", "wss") or not parsed.hostname):
                    raise ValueError("Thor地址应为 ws://主机:端口 或 wss://主机:端口")
                self.args.url = url or None
            taskless_teleop = self.selected_task is None and not initialize
            if not initialize and not taskless_teleop and self.mode in ("hil", "inference") and not (
                self.args.mock or self.args.url
            ):
                raise ValueError("请先填写Thor模型服务地址")
            # Freeze task identity for the entire arm/recording session.
            self.initializing = bool(initialize)
            self.taskless_teleop = taskless_teleop
            self._session_task = (
                {
                    "id": "device-initialization",
                    "name": "设备初始化",
                    "instruction": "只进行设备初始化与维护验收，不采集任务数据。",
                    "task": "Initialize and validate the robot station.",
                }
                if initialize
                else {
                    "id": "standalone-teleop",
                    "name": "遥操作",
                    "instruction": "双臂遥操作，不保存采集数据。",
                    "task": "Operate the bimanual robot without recording.",
                }
                if taskless_teleop
                else dict(self.selected_task)
            )
            self.task = self._session_task["task"]
            self.runtime = None
            self._previews = {}
            self._snapshot = {}
            self._camera_previous = {}
            self.error = None
            self.recording_error = None
            self.state = "connecting"
            self.heartbeat()
            self.log(
                "正在按初始化流程连接四台机械臂；连接后保持，不自动回零"
                if initialize
                else "正在连接四台机械臂进行遥操作；本会话不录制"
                if taskless_teleop
                else "正在独立连接四台机械臂；连接后保持，等待开始"
            )
            self.thread = threading.Thread(target=self._run, daemon=True, name="workstation-owner")
            self.thread.start()

    def _run(self):
        from ..resource_qos import place_on_cpus
        from .run import main

        argv = [
            "--station",
            self.args.station,
            "--mode",
            "collect" if self.initializing else "teleop" if self.taskless_teleop else self.mode,
            "--segment-seconds",
            str(getattr(self.args, "segment_seconds", 60)),
            "--min-free-gb",
            str(getattr(self.args, "min_free_gb", 0.5)),
        ]
        if self.args.mock:
            argv.append("--mock")
        if self.args.url:
            argv.extend(("--url", self.args.url))
        if self.args.baseline:
            argv.append("--baseline")
        try:
            # SDK CAN helper threads inherit the control owner's mask at creation.
            place_on_cpus("CONTROL")
            from ..config import build_station_config

            base = Path(self.args.output or build_station_config(self.args.station).save_root)
            if self.initializing:
                base = Path("data/workstation/initialization_sessions")
            elif self.taskless_teleop:
                base = Path("data/workstation/teleop_sessions")
            output = (
                base
                / self._session_task["id"]
                / (time.strftime("session_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6])
            )
            argv.extend(("--output", str(output)))
            main(argv, service=self)
        except (Exception, SystemExit) as exc:
            self.error = str(exc) or type(exc).__name__
            self.log("会话失败：" + self.error)
        finally:
            self.runtime = None
            self._previews = {}
            self.state = "fault" if self.error or self.cleanup_error else "disconnected"
            self.initializing = False
            self.taskless_teleop = False
            self.log("设备会话已结束" if not self.error else "请处理故障后重新连接")

    def attach(self, runtime, output):
        runtime.recording_allowed = not (self.initializing or self.taskless_teleop)
        runtime.prompt = self.task
        runtime.recorder.metadata["operator_task"] = self.task
        runtime.recorder.metadata["task"] = self.task
        runtime.recorder.metadata["collection_task"] = dict(self._session_task)
        runtime.recorder.metadata["station"]["task_name"] = self.task
        if self._profile_path.exists():
            try:
                profile = json.loads(self._profile_path.read_text())
                station_hash = hashlib.sha256(Path(self.args.station).read_bytes()).hexdigest()
                if profile["station_sha256"] == station_hash and profile["mock"] == self.args.mock:
                    runtime.maintenance.load(profile["pose"])
                    self.log("已加载与当前配置匹配的准备位")
            except (ValueError, KeyError, OSError) as exc:
                self.log("准备位读取失败：" + str(exc))
        self._saved_version = 0
        self.runtime, self.output = runtime, output
        self.state = "connected"
        if self._closing.is_set():
            runtime.event("quit")
        self.log(
            "机械臂已连接，当前保持；可开始遥操作"
            if self.taskless_teleop
            else "机械臂已连接，当前保持；相机就绪后可开始任务"
        )

    def saved(self):
        self.recording_error = self.runtime.recording_error if self.runtime else None
        self.runtime = None
        self._previews = {}
        self.state = "disconnected"
        self.log(
            "录制中断，机械臂已断开；请检查本集错误与已写入数据"
            if self.recording_error
            else "本地MP4与HDF5数据已保存；可上传服务器后独立转换"
        )

    def event(self, event):
        if event in ("preview_on", "preview_off"):
            self.preview_enabled = event == "preview_on"
            if not self.preview_enabled:
                self._previews = {}
            return
        if self.initializing and (
            event in ("start", "record", "takeover", "resume_policy")
            or event.startswith("mode:")
        ):
            raise ValueError("初始化会话只允许保持、重力补偿和有限设备调试")
        if event.startswith("mode:"):
            mode = Mode(event.split(":", 1)[1]).value
            with self._lock:
                if self.state in ("connecting", "finalizing", "disconnecting"):
                    raise ValueError("请等待当前连接或保存操作完成")
                if self.runtime is None:
                    self.mode = mode
                    self.log("已选择模式：" + mode)
                    return
        if self.runtime is None or self.state != "connected":
            raise ValueError("请先连接设备")
        runtime_mode = self.runtime.status.get("mode", self.mode)
        if event in ("record", "discard") and runtime_mode != "collect":
            raise ValueError("遥操作不提供录制功能；请切换到数据采集模式")
        needs_collection_context = (
            event in ("record", "resume_policy")
            or (event == "start" and runtime_mode != "teleop")
        ) and not (event == "record" and self.runtime.status.get("recording"))
        if needs_collection_context:
            if self.selected_task is None:
                raise ValueError("遥操作不录制；请断开机械臂并选择采集任务")
            if self.camera_state != "connected":
                raise ValueError("开始录制或模型执行前请先连接三路相机")
        updated = self.runtime.status.get("updated_at", 0)
        if event not in ("stop", "hold", "quit") and time.monotonic() - updated > 0.5:
            raise ValueError("控制状态尚未就绪或已过期，请先检查设备")
        self.runtime.event(event)
        self.log("指令已排队：" + event)

    def request_jog(self, arm, joint, delta):
        if self.runtime is None or self.state != "connected":
            raise ValueError("请先连接设备")
        if time.monotonic() - self._heartbeat > 3:
            raise ValueError("操作台心跳已断开")
        self.runtime.request_jog(arm, joint, delta)

    def disconnect(self, *, supported=False):
        if not self.args.mock and supported is not True:
            raise ValueError("断开可能结束力矩控制；请先支撑四台机械臂")
        with self._lock:
            if self.state == "connecting":
                raise ValueError("正在初始化，请等待完成；现场风险请使用物理急停")
            if self.runtime is None:
                raise ValueError("没有可断开的设备会话")
            if not self.initializing:
                self.mode = self.runtime.status.get("mode", self.mode)
            self.state = "disconnecting"
            self.runtime.event("quit")
            self.log("正在断开并保存当前会话")

    def preview(self, role):
        if (
            self.camera_state != "connected"
            or not self.preview_enabled
            or time.monotonic() - self._preview_at > 1
        ):
            return None
        camera = next((c for c in self._snapshot.get("cameras", []) if c["role"] == role), None)
        if not camera or not camera.get("healthy"):
            return None
        return self._previews.get(role)

    def _watchdog(self):
        while not self._closing.wait(0.2):
            runtime = self.runtime
            if runtime and time.monotonic() - self._heartbeat > 3 and not self._operator_lost:
                self._operator_lost = True
                runtime.event("hold")
                self.log("操作台心跳中断：已请求软件暂停，恢复连接不会自动运动")

    def _observe(self):
        try:
            self._observe_loop()
        except Exception as exc:
            self._previews = {}
            self.preview_enabled = False
            self.log("预览/健康采样已停止：" + str(exc))
            if self._encoder:
                self._encoder.close()
                self._encoder = None

    def _observe_loop(self):
        # Bound preview load independently of the 30 Hz acquisition/control streams.
        while not self._closing.wait(0.2):
            runtime = self.runtime
            if runtime is None and self.camera_state != "connected":
                if self._encoder:
                    self._encoder.close()
                    self._encoder = None
                continue
            if self._camera_generation != self._preview_runtime:
                if self._encoder:
                    self._encoder.close()
                    self._encoder = None
                self._previews = {}
                self._preview_at = 0.0
                self._preview_runtime = self._camera_generation
            now = time.monotonic()
            cameras, frames = [], {}
            for camera in self.camera_slots:
                frame = camera.read()
                if frame is None:
                    cameras.append({"role": camera.role, "healthy": False})
                    continue
                received = frame.meta.get("host_received_at", 0)
                sequence = frame.meta.get("sequence", 0)
                age = max(0, now - received)
                previous = self._camera_previous.get(camera.role)
                fps = 0.0
                if previous and received > previous[0]:
                    fps = (sequence - previous[1]) / (received - previous[0])
                self._camera_previous[camera.role] = (received, sequence)
                cameras.append(
                    {
                        "role": camera.role,
                        "age_s": age,
                        "fps": round(fps, 1),
                        "healthy": age <= (runtime.max_frame_age if runtime else 0.5),
                    }
                )
                rgb = frame.images.get("rgb")
                if rgb is not None and age <= (runtime.max_frame_age if runtime else 0.5):
                    frames[camera.role] = rgb
            if self.preview_enabled:
                if self._encoder is None:
                    from .preview import Preview

                    self._encoder = Preview()
                if not self._encoder.process.is_alive():
                    raise RuntimeError("预览编码进程已退出；控制和原始录制继续")
                self._encoder.submit(frames)
                result = self._encoder.poll()
                if result:
                    self._preview_at, self._previews = result
            elif self._encoder:
                self._encoder.close()
                self._encoder = None
            live = runtime.status if runtime else {}
            version = live.get("ready_version", 0)
            if version > self._saved_version and live.get("ready_pose"):
                try:
                    self._profile_path.parent.mkdir(parents=True, exist_ok=True)
                    temp = self._profile_path.with_suffix(".tmp")
                    temp.write_text(
                        json.dumps(
                            {
                                "pose": live["ready_pose"],
                                "mock": self.args.mock,
                                "station_sha256": hashlib.sha256(
                                    Path(self.args.station).read_bytes()
                                ).hexdigest(),
                            },
                            indent=2,
                        )
                    )
                    temp.replace(self._profile_path)
                    self._saved_version = version
                    self.log("准备位已保存到本机；回位前需确认运动路径清空")
                except OSError as exc:
                    self.log("准备位保存失败：" + str(exc))
            episodes = (
                list(getattr(runtime.recorder, "episodes", []))
                if runtime
                else list(self._snapshot.get("episodes", []))
            )
            root = Path(self.output or ".")
            try:
                free = disk_usage(root).free / 1024**3
            except OSError:
                free = None
            self._snapshot = {
                "health_sample_at": now,
                "cameras": cameras,
                "disk_free_gb": free,
                "episodes": episodes[-8:],
                "episode_count": sum(
                    e["outcome"] not in ("aborted", "discarded") for e in episodes
                ),
            }

    def close(self):
        self._closing.set()
        if self.runtime:
            self.runtime.event("quit")
        if self.thread:
            self.thread.join(timeout=60)
        if self.camera_thread:
            self.camera_thread.join(timeout=20)
        for slot in self.camera_slots:
            slot.worker = None
        self._close_cameras()
        self._monitor.join(timeout=4)
        self._watcher.join(timeout=1)
        if self._encoder:
            self._encoder.close()


def serve(args):
    import uvicorn

    from .web import create_app

    workbench = Workbench(args)
    try:
        uvicorn.run(
            create_app(workbench), host=args.web_host, port=args.web_port, log_level="warning"
        )
    finally:
        workbench.close()
