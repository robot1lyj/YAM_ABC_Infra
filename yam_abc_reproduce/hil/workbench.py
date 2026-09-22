"""Local operator service. Opening the UI never constructs hardware.

Device, camera and data-session lifecycles have separate workers. HTTP mutations
are serialized with nonblocking admission; motion commands reach the single
control owner. Preview encoding, writer recovery and disk queries stay off-loop.
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from ..storage_health import recording_storage_health
from .camera_slots import CameraSlot
from .core import Mode
from .data_session import paused_data_session, recover_recording
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
        self.recording_recovery = None
        self._recording_recovery_thread = None
        self.cleanup_error = None
        self.thread = None
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self._heartbeat = 0.0
        self._operator_lost = False
        self._snapshot = {}
        self._inventory_snapshot = {}
        self._health_error = None
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
        self._inventory_snapshot = inventory
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
        self.exit_initialization()
        return report

    def exit_initialization(self):
        """Continue on the powered session without claiming an acceptance result."""
        with self._editing():
            if not self.initializing or self.state != "connected" or self.runtime is None:
                raise ValueError("当前不在已连接的设备初始化会话")
            if (
                self.runtime.status.get("phase") != "hold"
                or self.runtime.status.get("maintenance") != "idle"
                or self.runtime.status.get("stop_latched")
                or self.runtime.recorder.recording
            ):
                raise ValueError("请先结束维护、解除锁存并保持机械臂")
            session = {
                "id": "standalone-teleop",
                "name": "遥操作",
                "instruction": "双臂遥操作，不保存采集数据。",
                "task": "Operate the bimanual robot without recording.",
            }
            self.initializing = False
            self.taskless_teleop = True
            self.mode = "teleop"
            self._session_task = session
            self.task = session["task"]
            self.runtime.recording_allowed = False
            self.runtime.prompt = session["task"]
            self.runtime.recorder.metadata["operator_task"] = session["task"]
            self.runtime.recorder.metadata["task"] = session["task"]
            self.runtime.recorder.metadata["collection_task"] = dict(session)
            self.runtime.recorder.metadata["station"]["task_name"] = session["task"]
            self.runtime.event("mode:teleop")
            self.log("已退出初始化向导；机械臂保持连接，可选择任务或遥操作")
            return {"mode": "teleop", "taskless_teleop": True}

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
            "camera_cleanup_pending": self.camera_state == "fault"
            and bool(getattr(self, "_camera_workers", [])),
            "video_backend": self.video_backend,
            "tasks": [dict(t) for t in self.tasks.items],
            "selected_task": None if self.selected_task is None else dict(self.selected_task),
            "task_error": self.tasks.error,
            "mock": self.args.mock,
            "mode": live.get("mode", self.mode),
            "connection_error": self.error,
            "recording_error": live.get("recording_error") or self.recording_error,
            "recording_recovery": self.recording_recovery,
            "cleanup_error": self.cleanup_error,
            "control_age_s": age,
            "operator_lost": self._operator_lost,
            "health_error": self._health_error,
            "health_age_s": max(0, time.monotonic() - sampled) if sampled else None,
            "health_monitor_alive": self._monitor.is_alive(),
            "policy_configured": bool(self.args.mock or self.args.url),
            "policy_url": live.get("policy_url", self.args.url or ""),
            "output": None if self.output is None else str(self.output),
            "events": list(self._log),
            "home_available": bool(live.get("ready_pose")),
            "preview_enabled": self.preview_enabled,
            "preview_age_s": max(0, time.monotonic() - self._preview_at)
            if self._preview_at
            else None,
            "home_reason": (
                "Follower 官方六关节零位；Leader 不主动运动，夹爪保持"
                if runtime and runtime.maintenance.factory_zero
                else "请先示教并保存四台机械臂的准备位"
            ),
            "factory_zero_home": bool(runtime and runtime.maintenance.factory_zero),
            "initializing": self.initializing,
            "taskless_teleop": self.taskless_teleop,
            "task_switching": bool(runtime and runtime.task_switching),
            "initialization": {**self._initialization, "inventory": dict(self._inventory_snapshot)},
        }

    def heartbeat(self):
        self._heartbeat = time.monotonic()
        self._operator_lost = False

    def _task_editable(self, *, first_binding=False):
        if self.thread and self.thread.is_alive():
            if (
                first_binding
                and self.state == "connected"
                and not self.initializing
                and self.runtime is not None
            ):
                if self.runtime.status.get("phase") != "hold":
                    raise ValueError("请先暂停遥操作，再选择采集任务；机械臂无需断开")
                if (
                    self.runtime.status.get("maintenance") != "idle"
                    or self.runtime.status.get("stop_latched")
                    or self.runtime.recording_error
                    or self.runtime.recorder.recording
                    or self.runtime.recorder.saving
                    or self.runtime.status.get("intervention_pending")
                    or self.runtime.task_switching
                ):
                    raise ValueError("请先结束介入、设备维护和录制，并等待保存完成")
                return
            raise ValueError("当前任务不可编辑；连接期间可在保持状态新建或切换任务")

    def _bind_task(self, task):
        if self.runtime is None:
            return
        from ..config import build_station_config

        base = Path(self.args.output or build_station_config(self.args.station).save_root)
        output = base / task["id"] / (
            self.output.name if self.taskless_teleop else
            time.strftime("session_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
        )
        metadata = {
            **self.runtime.recorder.metadata,
            "operator_task": task["task"],
            "task": task["task"],
            "collection_task": dict(task),
            "station": {
                **self.runtime.recorder.metadata["station"],
                "task_name": task["task"],
            },
        }
        for key in ("terminal_status", "omitted_intervention_waits", "close_errors"):
            metadata.pop(key, None)
        with paused_data_session(self.runtime) as change:
            if self.taskless_teleop:
                self.runtime.recorder.bind_task(output, metadata)
            else:
                self.runtime.recorder.rotate_task(output, metadata)
            self.runtime.prompt = task["task"]
            self.runtime.recording_allowed = True
            self.output = output
            self._session_task = dict(task)
            self.task = task["task"]
            self.taskless_teleop = False
            change.committed = True
        self.log("已切换数据会话；机械臂与相机保持连接")

    def restart_recording(self):
        """Queue explicit data-only recovery; the HTTP request never waits for disk."""
        with self._editing():
            runtime = self.runtime
            if (runtime is None or self.state != "connected" or self.initializing
                    or runtime.status.get("phase") != "hold"
                    or not (runtime.recording_error or runtime.recorder.error)):
                raise ValueError("请先暂停运动；此入口仅用于恢复故障录制，机械臂无需断开")
            if self._recording_recovery_thread and self._recording_recovery_thread.is_alive():
                raise ValueError("录制恢复正在进行，请等待结果")
            self.recording_recovery = {"state": "recovering", "error": None}
            self._recording_recovery_thread = threading.Thread(
                target=self._recover_recording, args=(runtime,), daemon=True,
                name="recording-recovery",
            )
            try:
                self._recording_recovery_thread.start()
            except Exception as exc:
                self.recording_recovery = {"state": "failed", "error": str(exc)}
                raise

    def _recover_recording(self, runtime):
        try:
            # Serializes task edits, not the status endpoint or urgent HOLD.
            with self._lock:
                if runtime is not self.runtime:
                    raise ValueError("设备会话已变化，未恢复录制")
                with paused_data_session(runtime, recovering=True) as change:
                    self.output = recover_recording(runtime, change)
                    self.recording_error = None
            self.recording_recovery = {"state": "complete", "error": None}
            self.log("录制已恢复到新数据会话，故障数据保留；机械臂保持，需手动开始")
        except Exception as exc:
            self.recording_recovery = {"state": "failed", "error": str(exc)}
            self.log("录制恢复未完成：" + str(exc))

    def create_task(self, name, instruction, task):
        with self._editing():
            self._task_editable(first_binding=True)
            previous = [dict(t) for t in self.tasks.items]
            selected = self.tasks.create(name, instruction, task)
            self._bind_catalog_task(selected, previous)
            self.selected_task = selected
            self.log("已创建并选择任务：" + self.selected_task["name"])
            return dict(self.selected_task)

    def update_task(self, task_id, name, instruction, task):
        with self._editing():
            self._task_editable(first_binding=True)
            previous = [dict(t) for t in self.tasks.items]
            selected = self.tasks.update(task_id, name, instruction, task)
            self._bind_catalog_task(selected, previous)
            self.selected_task = selected
            self.log("已更新并选择任务：" + self.selected_task["name"])
            return dict(self.selected_task)

    @contextmanager
    def _editing(self):
        if not self._lock.acquire(blocking=False):
            raise ValueError("另一个设备或任务操作正在处理；本次未提交，状态查看和暂停仍可用")
        try:
            yield
        finally:
            self._lock.release()

    def _bind_catalog_task(self, selected, previous):
        old_session = self._session_task
        try:
            self._bind_task(selected)
        except Exception:
            if self._session_task is not old_session:
                # Rotation committed, but release acknowledgement failed. Keep
                # catalog and selected identity consistent with the new session.
                self.selected_task = selected
            else:
                self.tasks.replace(previous)
            raise

    def select_task(self, task_id):
        with self._editing():
            self._task_editable(first_binding=True)
            selected = self.tasks.get(task_id)
            if not selected.get("task"):
                raise ValueError("请先补填当前任务的英文 task")
            self._bind_catalog_task(selected, [dict(t) for t in self.tasks.items])
            self.selected_task = selected
            self.log("已选择任务：" + self.selected_task["name"])
            return dict(self.selected_task)

    def connect_cameras(self):
        with self._editing():
            camera_thread = getattr(self, "camera_thread", None)
            if self.camera_state in ("connected", "connecting", "disconnecting") or (
                camera_thread and camera_thread.is_alive()
            ):
                raise ValueError("相机已连接或正在切换状态")
            if self._camera_workers:
                raise ValueError("上次相机关闭未完成，请先重试断开相机")
            self.camera_state, self.camera_error = "connecting", None
            self.log("正在独立连接三路相机，不启动机械臂")
            try:
                self.camera_thread = threading.Thread(
                    target=self._open_cameras, daemon=True, name="camera-owner"
                )
                self.camera_thread.start()
            except Exception as exc:
                self.camera_thread = None
                self.camera_state, self.camera_error = "fault", str(exc)
                self.log("相机连接失败：" + self.camera_error)
                raise

    def _open_cameras(self):
        drivers, workers = [], []
        try:
            from ..camera.worker import CameraWorker
            from ..config import build_station_config
            from ..runtime import build_cameras_from_config

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
            # A worker owns its driver even if start failed. Drivers not yet
            # wrapped also need retryable cleanup; neither may be forgotten.
            self._camera_workers = [*workers, *drivers[len(workers) :]]
            for cleanup in self._stop_camera_resources():
                self.camera_error += "; 关闭失败：" + cleanup
            self.camera_state = "fault"
            self.log("相机连接失败：" + self.camera_error)

    def disconnect_cameras(self):
        with self._editing():
            camera_thread = getattr(self, "camera_thread", None)
            if self.camera_state not in ("connected", "fault") or (
                camera_thread and camera_thread.is_alive()
            ):
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
            try:
                self.camera_thread = threading.Thread(
                    target=self._close_cameras, daemon=True, name="camera-close"
                )
                self.camera_thread.start()
            except Exception as exc:
                self.camera_thread = None
                self.camera_state, self.camera_error = "fault", str(exc)
                self.log("相机关闭失败：" + self.camera_error)
                raise

    def _stop_camera_resources(self):
        errors, pending = [], []
        for slot in self.camera_slots:
            slot.worker = None
        for worker in self._camera_workers:
            try:
                worker.stop()
            except Exception as exc:
                errors.append(str(exc))
                pending.append(worker)
        self._camera_workers = pending
        return errors

    def _close_cameras(self):
        errors = self._stop_camera_resources()
        self.video_backend = None
        self.camera_error = "; ".join(errors) or None
        self.camera_state = "fault" if errors else "disconnected"
        self.log("相机已断开" if not errors else "相机关闭失败：" + self.camera_error)

    def connect(self, *, ready=False, initialize=False, url=None):
        with self._editing():
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
            # Task identity belongs to a data session, not the SDK connection.
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
            self.recording_recovery = None
            self.state = "connecting"
            self.heartbeat()
            self.log(
                "正在按初始化流程连接四台机械臂；连接后保持，不自动回零"
                if initialize
                else "正在连接四台机械臂进行遥操作；本会话不录制"
                if taskless_teleop
                else "正在独立连接四台机械臂；连接后保持，等待开始"
            )
            try:
                self.thread = threading.Thread(
                    target=self._run, daemon=True, name="workstation-owner"
                )
                self.thread.start()
            except Exception as exc:
                self.thread = None
                self.state, self.error = "fault", str(exc)
                self.initializing = self.taskless_teleop = False
                self.log("会话启动失败：" + self.error)
                raise

    def _run(self):
        try:
            self._run_session()
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

    def _run_session(self):
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
        if getattr(self.args, "executor_socket", None):
            argv.extend(("--executor-socket", self.args.executor_socket))
        for option, value in (
            ("--policy-fusion", getattr(self.args, "policy_fusion", None)),
            ("--action-dt", getattr(self.args, "action_dt", None)),
            ("--expected-policy-latency", getattr(self.args, "expected_policy_latency", None)),
            ("--prefetch-margin", getattr(self.args, "prefetch_margin", None)),
        ):
            if value is not None:
                argv.extend((option, str(value)))
        # SDK CAN helper threads inherit the control owner's mask at creation.
        place_on_cpus("CONTROL")
        from ..config import build_station_config

        base = Path(self.args.output or build_station_config(self.args.station).save_root)
        if self.initializing:
            base = base.parent / "workstation" / "initialization_sessions"
        elif self.taskless_teleop:
            # Keep the unrecorded session on the task output's filesystem
            # so the first task binding can atomically rename it.
            base = base.parent / "workstation" / "teleop_sessions"
        output = (
            base
            / self._session_task["id"]
            / (time.strftime("session_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6])
        )
        argv.extend(("--output", str(output)))
        main(argv, service=self)

    def attach(self, runtime, output):
        runtime.recording_allowed = not (self.initializing or self.taskless_teleop)
        runtime.prompt = self.task
        runtime.recorder.metadata["operator_task"] = self.task
        runtime.recorder.metadata["task"] = self.task
        runtime.recorder.metadata["collection_task"] = dict(self._session_task)
        runtime.recorder.metadata["station"]["task_name"] = self.task
        if not runtime.maintenance.factory_zero and self._profile_path.exists():
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
        retained = bool(getattr(self.args, "executor_socket", None)) and not getattr(
            getattr(self.runtime, "io", None), "release_on_close", False
        )
        self.recording_error = self.runtime.recording_error if self.runtime else None
        self.runtime = None
        self._previews = {}
        self.state = "disconnected"
        if retained:
            self.log("会话已结束；SDK执行层未随会话关闭，请检查执行层保持状态后重新接入")
            return
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
            with self._editing():
                if self.state in ("connecting", "finalizing", "disconnecting"):
                    raise ValueError("请等待当前连接或保存操作完成")
                if self.runtime is not None and mode != "teleop" and self.selected_task is None:
                    raise ValueError("请先暂停遥操作并选择采集任务；机械臂无需断开")
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
                raise ValueError("遥操作不录制；请先暂停并选择采集任务")
            if self.camera_state != "connected":
                raise ValueError("开始录制或模型执行前请先连接三路相机")
        updated = self.runtime.status.get("updated_at", 0)
        if event not in ("stop", "hold", "quit") and time.monotonic() - updated > 0.5:
            raise ValueError("控制状态尚未就绪或已过期，请先检查设备")
        self.runtime.event(event)
        self.log("指令已排队：" + event)

    def request_jog(self, arm, joint, delta=None, *, target=None):
        if self.runtime is None or self.state != "connected":
            raise ValueError("请先连接设备")
        if time.monotonic() - self._heartbeat > 3:
            raise ValueError("操作台心跳已断开")
        self.runtime.request_jog(arm, joint, delta, target=target)

    def configure_policy(self, *, fusion, rtc_delay_steps=None):
        if self.runtime is None or self.state != "connected" or self.initializing:
            raise ValueError("请先连接设备并退出初始化向导")
        self.runtime.configure_policy(fusion=fusion, rtc_delay_steps=rtc_delay_steps)
        self.log(f"推理动作块设置已提交：{fusion}")

    def restart_policy(self):
        if self.runtime is None or self.state != "connected" or self.initializing:
            raise ValueError("请先连接设备并退出初始化向导")
        self.runtime.restart_policy()
        self.log("已请求重载推理通信子进程；机械臂保持连接")

    def reload_interaction(self):
        if self.runtime is None or self.state != "connected" or self.initializing:
            raise ValueError("请先连接设备并退出初始化向导")
        self.runtime.reload_interaction()
        self.log("已请求HOLD下重载交互规则；不关闭SDK或释放力矩")

    def change_policy_source(self, *, url):
        parsed = urlparse(url)
        if parsed.scheme not in ("ws", "wss") or not parsed.hostname:
            raise ValueError("来源地址需为 ws:// 或 wss://")
        with self._editing():
            if self.initializing or self.state not in ("disconnected", "connected"):
                raise ValueError("请等待设备操作完成并退出初始化向导")
            if self.runtime is None and self.state == "disconnected":
                self.args.url = url
                self.log("已配置推理来源；尚未连接或验证模型服务")
                return
            self.runtime.change_policy_source(url=url)
            self.args.url = url
            self.log("已请求切换推理来源；只重建通信子进程，机械臂保持连接")

    def restart_planner(self):
        if self.runtime is None or self.state != "connected" or self.initializing:
            raise ValueError("请先连接设备并退出初始化向导")
        self.runtime.restart_planner()
        self.log("已请求重载动作规划子进程；机械臂保持连接")

    def disconnect(self, *, supported=False):
        if not self.args.mock and supported is not True:
            raise ValueError("断开可能结束力矩控制；请先支撑四台机械臂")
        with self._editing():
            if self.state == "connecting":
                raise ValueError("正在初始化，请等待完成；现场风险请使用物理急停")
            if self.runtime is None:
                raise ValueError("没有可断开的设备会话")
            if not self.initializing:
                self.mode = self.runtime.status.get("mode", self.mode)
            self.state = "disconnecting"
            if hasattr(self.runtime.io, "release_on_close"):
                self.runtime.io.release_on_close = True
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
        last_error = None
        while not self._closing.wait(0.2):
            runtime = self.runtime
            if runtime and time.monotonic() - self._heartbeat > 3 and not self._operator_lost:
                try:
                    runtime.event("hold")
                except Exception as exc:
                    if str(exc) != last_error:
                        self.log("心跳暂停请求失败，请使用实体急停：" + str(exc))
                    last_error = str(exc)
                    continue
                self._operator_lost = True
                last_error = None
                self.log("操作台心跳中断：已请求软件暂停，恢复连接不会自动运动")

    def _observe(self):
        while not self._closing.is_set():
            try:
                self._observe_loop()
            except Exception as exc:
                self._previews = {}
                message = str(exc)
                if message != self._health_error:
                    self.log("健康采样异常，稍后重试：" + message)
                self._health_error = message
                self._closing.wait(1)

    def _observe_loop(self):
        from ..config import build_station_config

        # Bound preview load independently of the 30 Hz acquisition/control streams.
        while not self._closing.wait(0.2):
            runtime = self.runtime
            self._inventory_snapshot = self._station_inventory()
            # Also report a missing data disk before any hardware is connected.
            root = Path(
                self.output or getattr(self.args, "output", None)
                or build_station_config(self._station_path()).save_root
            )
            storage = recording_storage_health(root)
            free_bytes = storage.get("free_bytes")
            free = free_bytes / 1024**3 if free_bytes is not None else None
            self._snapshot.update(recording_storage=storage, disk_free_gb=free)
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
                try:
                    if self._encoder is None:
                        from .preview import Preview

                        self._encoder = Preview()
                    if not self._encoder.process.is_alive():
                        raise RuntimeError("预览编码进程已退出")
                    self._encoder.submit(frames)
                    result = self._encoder.poll()
                    if result:
                        self._preview_at, self._previews = result
                except Exception as exc:
                    self.preview_enabled = False
                    self._previews = {}
                    self._preview_at = 0.0
                    self.log("预览已停止，健康采样和控制继续：" + str(exc))
                    encoder, self._encoder = self._encoder, None
                    if encoder:
                        try:
                            encoder.close()
                        except Exception as close_exc:
                            self.log("预览清理失败：" + str(close_exc))
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
            self._snapshot = {
                "health_sample_at": now,
                "cameras": cameras,
                "disk_free_gb": free,
                "recording_storage": storage,
                "episodes": episodes[-8:],
                "episode_count": sum(
                    e["outcome"] not in ("aborted", "discarded") for e in episodes
                ),
            }
            self._health_error = None

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
        if self.camera_thread and self.camera_thread.is_alive():
            # The existing owner still owns startup/cleanup. Do not call stop
            # concurrently; _open_cameras observes _closing before publication.
            self.camera_error = "相机连接或清理线程尚未退出，未重复关闭设备"
            self.log(self.camera_error)
        else:
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
            create_app(workbench, control_access=not args.mock), host=args.web_host, port=args.web_port, log_level="warning"
        )
    finally:
        workbench.close()


def serve_device(args):
    """Run the persistent hardware owner on a private Unix-domain socket."""
    import uvicorn

    from .web import create_app

    socket_path = Path(args.device_socket)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass
    workbench = Workbench(args)
    try:
        uvicorn.run(create_app(workbench, control_access=False), uds=str(socket_path), log_level="warning")
    finally:
        workbench.close()
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass


def serve_web(args):
    """Run the restartable public UI/API without constructing any hardware."""
    import uvicorn

    from .device_client import DeviceClient
    from .web import create_app

    client = DeviceClient(args.device_socket, args)
    client.hold_on_attach()
    uvicorn.run(
        create_app(client, control_access=not args.mock), host=args.web_host, port=args.web_port, log_level="warning"
    )
