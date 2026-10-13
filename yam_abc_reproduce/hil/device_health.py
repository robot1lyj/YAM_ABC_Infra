"""Read-only start checks and conservative joint non-following detection.

The detector compares feedback with the PREVIOUS submitted command, never the
next prediction. It cannot certify motion while stationary or diagnose a motor
from LED colour. No CAN traffic, SDK writes, auto-reset or action reshaping here.
"""
import math
import time
from collections import deque

import numpy as np

MOTION_EVENTS = frozenset({"start", "resume_policy", "manual_ready", "takeover",
                           "home", "home_leader", "gravity"})
ERRORS = {0: "未使能", 1: "正常", 8: "过压", 9: "欠压", 10: "过流",
          11: "驱动过温", 12: "电机过温", 13: "通信丢失", 14: "过载"}


class DeviceHealth:
    # Diagnostic thresholds, not target clipping or motor protection ratings.
    ERROR_RAD = math.radians(10)
    MOTION_RAD = math.radians(1)
    WINDOW_S = 1.0

    def __init__(self, max_age=.25):
        self.max_age = max_age
        self.checked_at = None
        self.arms = []
        self.issues = ["等待设备反馈检查"]
        self.tracking_fault = None
        self._windows = [deque() for _ in range(24)]
        self._previous = None

    def update(self, arms, *, now, follower, leader):
        self.checked_at, self.arms = now, arms
        issues = [] if len(arms) == 4 else ["缺少四臂设备诊断反馈"]
        for arm in arms:
            label = arm["label"]
            if not arm["running"]:
                issues.append(f"{label} SDK 控制线程已停止；暂停并检查供电、USB-CAN 后重连")
            age = arm["feedback_age_s"]
            if age is None or not math.isfinite(age) or not 0 <= age <= self.max_age:
                issues.append(f"{label} 反馈超时或无效；检查通信，不能开始运动")
            if not arm["motors"]:
                issues.append(f"{label} 缺少电机诊断反馈")
            for motor in arm["motors"]:
                code = motor["error_code"]
                if code != 1:
                    desc = ERRORS.get(code, f"未知报警 {code}")
                    issues.append(f"{label} {motor['joint']}：{desc}；暂停并检查设备，不要反复启动")
        self.issues = issues
        if self._previous is None or issues:
            self.clear_window()
            return
        target, enabled, submitted_at = self._previous
        measured = np.concatenate((follower, leader))
        # A delayed loop is not evidence of a mechanically stuck joint.
        if not 0 <= now - submitted_at <= self.max_age:
            self.clear_window()
            return
        indices = [i for i in range(28) if i % 7 != 6]
        for n, i in enumerate(indices):
            window = self._windows[n]
            error = target[i] - measured[i]
            if not enabled[i // 7] or abs(error) <= self.ERROR_RAD:
                window.clear()
                continue
            if window and np.sign(error) != window[-1][2]:
                window.clear()
            window.append((now, float(measured[i]), float(np.sign(error))))
            while len(window) > 1 and window[1][0] <= now - self.WINDOW_S:
                window.popleft()
            if now - window[0][0] < self.WINDOW_S:
                continue
            movement = max(v[1] for v in window) - min(v[1] for v in window)
            if movement < self.MOTION_RAD and self.tracking_fault is None:
                side = "左" if (i // 7) % 2 == 0 else "右"
                role = "Follower" if i < 14 else "Leader"
                self.tracking_fault = dict(
                    arm=f"{side} {role}", joint=f"J{i % 7 + 1}",
                    error_deg=math.degrees(abs(error)), movement_deg=math.degrees(movement),
                    target_rad=float(target[i]), measured_rad=float(measured[i]),
                    detected_at=now,
                    message=(f"{side} {role} J{i % 7 + 1} 疑似不跟随：偏差 {math.degrees(abs(error)):.1f}°，持续超过 10° 达 1 秒，"
                             "实测位移不足 1°。已请求暂停；检查红灯、供电与连接，处理后检查并确认。"),
                )

    def submitted(self, follower, leader, *, enabled, now):
        self._previous = (np.concatenate((follower, leader)), enabled, now)

    def clear_window(self):
        self._previous = None
        for window in self._windows:
            window.clear()

    def blocking_reason(self, *, now=None):
        now = time.monotonic() if now is None else now
        if self.checked_at is None or not 0 <= now - self.checked_at <= self.max_age:
            return "设备健康状态尚未更新或已过期；请检查连接，等待新鲜反馈"
        if self.issues:
            return "；".join(self.issues)
        return self.tracking_fault["message"] if self.tracking_fault else None

    def acknowledge(self, *, now):
        # Explicit acknowledgement is NOT proof that motion has recovered.
        fault = self.tracking_fault
        self.tracking_fault = None
        reason = self.blocking_reason(now=now)
        if reason:
            self.tracking_fault = fault
            raise ValueError(reason)
        self.clear_window()

    def snapshot(self, *, now=None):
        reason = self.blocking_reason(now=now)
        return dict(ok=reason is None, checked_at=self.checked_at, reason=reason,
                    arms=self.arms, tracking_fault=self.tracking_fault,
                    thresholds=dict(error_deg=10, duration_s=1, movement_deg=1),
                    scope="只读通信与报警检查；静止通过不代表运动跟随已验证")
