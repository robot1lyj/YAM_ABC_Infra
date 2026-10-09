"""Bounded HIL Cartesian steps. IK computes targets; only Runtime writes SDK.

One job may be outstanding. Epoch/generation and expiry checks quarantine late
solutions after pause, handback or reset. Each worker owns its mutable IK model.
This is a position jog, not force control, collision avoidance or a servo loop.
"""

import queue
import threading
import time

import numpy as np


class CartesianJog:
    MAX_STEP_M = .007
    MAX_GRIPPER_STEP = .1
    MAX_JOINT_STEP_RAD = .08
    MAX_TRACKING_ERROR_RAD = .15
    RESULT_TIMEOUT_S = .5

    def __init__(self, kinematics_factory=None):
        self.factory = kinematics_factory
        self.jobs = queue.Queue(maxsize=1)
        self.results = queue.SimpleQueue()
        self.closed = threading.Event()
        self.generation = 0
        self.pending = None
        self.last_command_id = 0
        self.error = None
        self.applied = None
        self.thread = None
        self._prepare_lock = threading.Lock()
        self.ready = threading.Event()
        self.model_error = None

    def prepare(self):
        """Warm FK/IK while HOLD; model loading never occupies a control tick."""
        with self._prepare_lock:
            if self.closed.is_set():
                raise ValueError("微调会话已关闭，请重新连接")
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, daemon=True, name="hil-cartesian-ik")
                self.thread.start()

    @staticmethod
    def validate(arm, axis, delta):
        if arm not in ("left", "right") or axis not in ("x", "y", "z", "gripper"):
            raise ValueError("请选择左/右臂的 XYZ 或夹爪")
        limit = CartesianJog.MAX_GRIPPER_STEP if axis == "gripper" else CartesianJog.MAX_STEP_M
        if not np.isfinite(delta) or not 0 < abs(delta) <= limit + 1e-12:
            raise ValueError("末端单步最多7毫米，夹爪单步最多10%")

    def cancel(self):
        self.generation += 1
        self.pending = self.applied = None
        while True:
            try:
                self.jobs.get_nowait()
            except queue.Empty:
                break
        while True:
            try:
                self.results.get_nowait()
            except queue.Empty:
                break

    def request(self, *, arm, axis, delta, epoch, command_id, target, measured, now):
        self.validate(arm, axis, delta)
        target, measured = np.asarray(target), np.asarray(measured)
        if (target.shape != (14,) or measured.shape != (14,)
                or not np.isfinite(target).all() or not np.isfinite(measured).all()):
            raise ValueError("微调目标或机械臂反馈无效，该步未执行")
        self.prepare()
        if axis != "gripper" and not self.ready.is_set():
            raise ValueError("末端计算正在准备，请稍候再点动")
        if command_id <= self.last_command_id:
            raise ValueError("微调指令已过期，请使用最新面板状态")
        self.last_command_id = command_id
        if self.pending is not None:
            raise ValueError("正在计算上一小步，请稍候；未积压本次指令")
        offset = 0 if arm == "left" else 7
        if np.max(np.abs(target[offset:offset+6] - measured[offset:offset+6])) > self.MAX_TRACKING_ERROR_RAD:
            raise ValueError("机械臂尚未跟上微调目标，请暂停并检查反馈")
        job = dict(arm=arm, axis=axis, delta=float(delta), epoch=epoch,
                   command_id=command_id, generation=self.generation,
                   requested_at=now, target=target.copy())
        self.pending = job
        self.error = None
        self.jobs.put_nowait(job)

    def _run(self):
        kinematics = None
        try:
            if self.factory is None:
                from .kinematics import DualArmEefConverter
                kinematics = DualArmEefConverter()
            else:
                kinematics = self.factory()
        except Exception as exc:
            self.model_error = f"末端运动学不可用：{exc}"
        finally:
            self.ready.set()
        while not self.closed.is_set():
            try:
                job = self.jobs.get(timeout=.1)
            except queue.Empty:
                continue
            result = dict(job)
            try:
                target = job["target"].copy()
                offset = 0 if job["arm"] == "left" else 7
                if job["axis"] == "gripper":
                    target[offset+6] = np.clip(target[offset+6] + job["delta"], 0, 1)
                else:
                    if self.model_error:
                        raise ValueError(self.model_error)
                    arm = getattr(kinematics, job["arm"])
                    seed = target[offset:offset+6].copy()
                    pose = arm.fk(seed).copy()
                    pose["xyz".index(job["axis"]), 3] += job["delta"]
                    solution = np.asarray(arm.ik(pose, seed))
                    if (solution.shape != (6,) or not np.isfinite(solution).all()
                            or np.max(np.abs(solution-seed)) > self.MAX_JOINT_STEP_RAD):
                        raise ValueError("逆解关节变化过大，可能接近奇异位置；该步未执行")
                    target[offset:offset+6] = solution
                    result["target_pose"] = pose.tolist()
                result["target"] = target
                result["error"] = None
            except Exception as exc:
                result["error"] = f"末端微调未执行：{exc}"
            result["completed_at"] = time.monotonic()
            self.results.put(result)

    def poll(self, epoch, now):
        accepted = None
        while True:
            try:
                result = self.results.get_nowait()
            except queue.Empty:
                break
            if (self.pending is None or result["generation"] != self.generation
                    or result["epoch"] != epoch
                    or result["command_id"] != self.pending["command_id"]):
                continue
            self.pending = None
            if now - result["requested_at"] > self.RESULT_TIMEOUT_S:
                self.error = "末端逆解超时，该步未执行；请重新点动"
            elif result["error"]:
                self.error = result["error"]
            else:
                self.applied = result
                accepted = result["target"].copy()
        if self.pending is not None and now-self.pending["requested_at"] > self.RESULT_TIMEOUT_S:
            self.cancel()
            self.error = "末端逆解超时，该步未执行；请重新点动"
        return accepted

    def snapshot(self):
        return dict(busy=self.pending is not None, error=self.error or self.model_error,
                    warming=self.thread is not None and not self.ready.is_set(),
                    last_command_id=self.last_command_id,
                    applied=None if self.applied is None else {
                        k: v for k, v in self.applied.items() if k != "target"
                    }, step_limit_mm=self.MAX_STEP_M*1000)

    def close(self):
        self.cancel()
        self.closed.set()
