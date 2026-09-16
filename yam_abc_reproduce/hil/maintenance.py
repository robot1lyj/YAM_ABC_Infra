"""Exclusive recovery/teaching control, evaluated by the same 30 Hz owner.

Factory home means six zero arm joints, never motor encoder recalibration.
Stations without factory-zero mode may still use a taught ready pose.
"""

import numpy as np

from .core import vector


class Maintenance:
    HOME_SPEED = 0.12
    # Loaded joints can normally trail the commanded interpolation by a little
    # over 0.08 rad.  Keep enough room for that measured lag while retaining a
    # separate 0.15 rad hard stop for a genuine tracking anomaly.
    HOME_TRACKING_WINDOW = 0.12
    HOME_HARD_ERROR = 0.15
    HOME_STALL_TIMEOUT = 5.0

    def __init__(self, *, factory_zero=False):
        self.factory_zero = factory_zero
        self.latched = False
        self.state = "idle"
        self.ready = (
            {"follower": np.zeros(14).tolist(), "leader": np.zeros(14).tolist()}
            if factory_zero
            else None
        )
        self.ready_version = 0
        self.started = 0.0
        self.error = None
        self.grippers = None
        self.frozen = None
        self.home_start = None
        self.home_duration = 0.0
        self.home_progress = 0.0
        self.home_stalled_at = None

    def capture(self, q, leader):
        if self.factory_zero:
            raise ValueError("此设备固定使用官方关节零位，无需保存准备位")
        self.ready = {"follower": vector(q).tolist(), "leader": vector(leader).tolist()}
        self.ready_version += 1

    def load(self, profile):
        if self.factory_zero:
            return
        self.ready = {key: vector(profile[key]).tolist() for key in ("follower", "leader")}

    def command(self, event, q, leader, *, now, paused):
        if event == "stop":
            self.latched = True
            self.error = None
            self.frozen = (q.copy(), leader.copy())
            self.state = "idle"
            self.home_start = None
            self.home_progress = 0.0
            self.home_stalled_at = None
            return "hold"
        if event == "reset_stop":
            self.latched = False
            self.state = "idle"
            self.error = None
            self.frozen = None
            self.home_progress = 0.0
            self.home_stalled_at = None
            return "hold"
        if event == "hold" or (event and event.startswith("mode:")):
            self.state = "idle"
            self.home_start = None
            self.home_progress = 0.0
            self.home_stalled_at = None
            self.error = None
        if self.latched:
            return "hold"
        if event == "capture_home":
            if not paused or self.state != "idle":
                raise ValueError("请先暂停并结束录制，再保存准备位")
            self.capture(q, leader)
            return "hold"
        if event in ("home", "gravity"):
            if not paused:
                raise ValueError("请先暂停并结束录制")
            if event == "home" and self.ready is None:
                raise ValueError("请先示教并保存准备位")
            self.state = "homing" if event == "home" else "gravity"
            self.started = now
            self.error = None
            self.grippers = q[[6, 13]].copy()
            if event == "home":
                target = vector(self.ready["follower"])
                lead_target = vector(self.ready["leader"])
                joints = [0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12]
                distance = np.max(np.abs(target[joints] - q[joints]))
                if not self.factory_zero:
                    distance = max(
                        distance,
                        np.max(np.abs(lead_target[joints] - leader[joints])),
                    )
                self.home_start = (q.copy(), None if self.factory_zero else leader.copy())
                self.home_duration = distance / self.HOME_SPEED
                self.home_progress = 0.0
                self.home_stalled_at = None
            return "hold"
        if self.state != "idle":
            return "hold"
        return event

    def step(self, q, leader, *, now, dt):
        if self.latched:
            return self.frozen
        if self.state != "homing":
            return None
        if now - self.started > 60:
            self.state = "idle"
            self.home_start = None
            self.home_stalled_at = None
            self.error = "回准备位超时，已暂停；请检查阻挡和反馈"
            return None
        target, lead_target = vector(self.ready["follower"]), vector(self.ready["leader"])
        # Home arm joints only. Never open a gripper holding a part.
        target[[6, 13]] = self.grippers
        lead_target[[6, 13]] = leader[[6, 13]]
        joints = [0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12]
        distance = np.max(np.abs(target[joints] - q[joints]))
        if not self.factory_zero:
            distance = max(
                distance,
                np.max(np.abs(lead_target[joints] - leader[joints])),
            )
        if distance <= 0.015:
            self.state = "idle"
            self.home_start = None
            return None
        start, lead_start = self.home_start
        current = start + (target - start) * self.home_progress
        lead_current = (
            None
            if self.factory_zero
            else lead_start + (lead_target - lead_start) * self.home_progress
        )

        def tracking_error(follower_plan, leader_plan):
            errors = np.abs(follower_plan[joints] - q[joints])
            owner, local = "Follower", int(np.argmax(errors))
            maximum = float(errors[local])
            dimension = joints[local]
            if leader_plan is not None:
                leader_errors = np.abs(leader_plan[joints] - leader[joints])
                leader_local = int(np.argmax(leader_errors))
                if float(leader_errors[leader_local]) > maximum:
                    owner, local = "Leader", leader_local
                    maximum = float(leader_errors[leader_local])
                    dimension = joints[leader_local]
            side = "左" if dimension < 7 else "右"
            joint = dimension + 1 if dimension < 7 else dimension - 6
            return maximum, f"{side}{owner} J{joint}"

        # Keep the last bounded target if feedback is slower than the nominal
        # interpolation.  This still accumulates enough position error to cross
        # motor deadband, but it cannot run an otherwise healthy loaded arm into
        # the old 0.15 rad tracking abort merely because the wall clock advanced.
        current_error, current_joint = tracking_error(current, lead_current)
        if current_error > self.HOME_HARD_ERROR:
            self.state = "idle"
            self.home_start = None
            self.home_stalled_at = None
            self.error = f"回零反馈异常，已暂停：{current_joint} 落后 {current_error:.3f} rad"
            return None

        candidate_progress = min(
            1.0,
            self.home_progress + dt / max(self.home_duration, dt),
        )
        candidate = start + (target - start) * candidate_progress
        lead_candidate = (
            None
            if self.factory_zero
            else lead_start + (lead_target - lead_start) * candidate_progress
        )
        candidate_error, candidate_joint = tracking_error(candidate, lead_candidate)
        if candidate_error <= self.HOME_TRACKING_WINDOW:
            self.home_progress = candidate_progress
            self.home_stalled_at = None
            planned, lead_planned = candidate, lead_candidate
        else:
            planned, lead_planned = current, lead_current
            if self.home_stalled_at is None:
                self.home_stalled_at = now
            elif now - self.home_stalled_at >= self.HOME_STALL_TIMEOUT:
                self.state = "idle"
                self.home_start = None
                self.home_stalled_at = None
                self.error = (
                    f"回零反馈停滞，已暂停：{candidate_joint} 落后 {candidate_error:.3f} rad"
                )
                return None

        # Completion is based on the governed trajectory, not wall time.  With
        # the bounded tracking window, the final target reaches the common outer
        # command clamp intact and is then retained by Runtime.hold().
        if self.home_progress >= 1.0:
            self.state = "idle"
            self.home_start = None
            self.home_stalled_at = None
        return planned, lead_planned
