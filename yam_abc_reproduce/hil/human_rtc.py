"""Client-owned handover motion, submitted through the existing trained RTC.

Only an explicit handback makes the short continuation executable. Human motion
is never frozen or predicted into the execution timeline during free teleop.
"""

from collections import deque
from dataclasses import dataclass

import numpy as np

SETTINGS = frozenset({"enable_human_rtc", "human_rtc_velocity_decay_steps",
    "human_rtc_max_velocity", "human_rtc_max_sample_age"})


@dataclass(frozen=True)
class HumanRtcConfig:
    enabled: bool = True
    velocity_decay_steps: int = 6  # 0.2 seconds at the station's 30 Hz.
    max_velocity: float = 2.0  # rad/s; grippers retain their last submitted opening.
    max_sample_age: float = 0.15

    @classmethod
    def from_settings(cls, settings):
        cfg = cls(**{("enabled" if key == "enable_human_rtc" else key.removeprefix("human_rtc_")): value
                     for key, value in settings.items() if key in SETTINGS})
        if type(cfg.enabled) is not bool:
            raise ValueError("enable_human_rtc must be true or false")
        if type(cfg.velocity_decay_steps) is not int or not 1 <= cfg.velocity_decay_steps <= 10:
            raise ValueError("human_rtc_velocity_decay_steps must be an integer from 1 to 10")
        for name in ("max_velocity", "max_sample_age"):
            value = getattr(cfg, name)
            if type(value) not in (float, int) or not np.isfinite(value) or value <= 0:
                raise ValueError(f"human_rtc_{name} must be finite and positive")
        return cfg


def build_handover_targets(latest, previous, *, sample_dt, action_dt, steps, config, limit_target):
    """Continue the submitted joint velocity, tapering it to zero; hold grippers."""
    latest, previous = np.asarray(latest, dtype=float), np.asarray(previous, dtype=float)
    if (latest.shape != (14,) or previous.shape != (14,)
            or not np.isfinite(latest).all() or not np.isfinite(previous).all()
            or not np.isfinite(sample_dt) or sample_dt <= 0
            or not np.isfinite(action_dt) or action_dt <= 0
            or type(steps) is not int or steps < 1):
        raise ValueError("invalid handover samples/timebase")
    velocity = np.clip((latest - previous) / sample_dt, -config.max_velocity, config.max_velocity)
    velocity[[6, 13]] = 0
    target = latest.copy()
    targets = []
    for step in range(1, steps + 1):
        u = step / steps
        target = np.asarray(limit_target(target + velocity * (1 - 3*u*u + 2*u*u*u) * action_dt)).copy()
        targets.append(target)
    return np.asarray(targets)


class HumanRtc:
    """The control owner supplies actual successful SDK targets and timestamps."""

    def __init__(self, config, *, action_dt=1/30):
        self.config, self.action_dt = config, action_dt
        self.samples = deque(maxlen=2)
        self.epoch = None
        self.active = False
        self.reason = "等待推理通信就绪"
        self.handovers = 0

    def refresh(self, arbiter, *, leaders_connected=True, pld=False, ready=True):
        self.active = False
        if not self.config.enabled:
            self.reason = "趋势延续已关闭；交还使用普通RTC保持前缀"
        elif (arbiter.mode != "hil" or arbiter.hil_input != "leader"
              or arbiter.rtc_timeline is None or not leaders_connected or pld):
            self.reason = "趋势延续仅用于Leader HIL、原生RTC、PLD关闭"
        elif not ready:
            self.reason = "推理通信尚未就绪"
        else:
            self.active, self.reason = True, None
        if not self.active:
            self.samples.clear()

    def record_submitted(self, arbiter, action, *, tick, now):
        if self.epoch != arbiter.epoch:
            self.samples.clear()
            self.epoch = arbiter.epoch
        if not self.active or arbiter.phase != "human":
            self.samples.clear()
            return
        if self.samples and (tick != self.samples[-1][0] + 1
                or not 0 < now - self.samples[-1][1] <= self.config.max_sample_age):
            self.samples.clear()
        self.samples.append((tick, now, np.asarray(action).copy()))

    def prepare(self, arbiter, *, tick, now, limit_target):
        if (not self.active or arbiter.phase != "human" or self.epoch != arbiter.epoch
                or len(self.samples) != 2 or tick is None):
            return None
        old, latest = self.samples
        if latest[0] != tick - 1 or not 0 <= now - latest[1] <= self.config.max_sample_age:
            return None
        steps = min(self.config.velocity_decay_steps, arbiter.rtc_timeline.delay_steps)
        targets = build_handover_targets(latest[2], old[2], sample_dt=latest[1] - old[1],
            action_dt=self.action_dt, steps=steps, config=self.config, limit_target=limit_target)
        context = dict(source_epoch=arbiter.epoch, start_tick=tick,
            velocity_decay_steps=steps, max_velocity=self.config.max_velocity,
            samples=[dict(tick=k, submitted_at=t, submitted_action=q.copy()) for k, t, q in self.samples])
        return targets, context

    def after_transition(self, arbiter, prepared):
        if self.epoch != arbiter.epoch:
            self.samples.clear()
            self.epoch = arbiter.epoch
        if prepared is not None and arbiter.phase == "resume":
            targets, context = prepared
            arbiter.rtc_timeline.seed_handover(context["start_tick"], targets, context=context)
            self.handovers += 1

    def snapshot(self):
        return dict(configured=self.config.enabled, available=self.active, reason=self.reason,
            handovers=self.handovers, velocity_decay_steps=self.config.velocity_decay_steps,
            velocity_decay_seconds=self.config.velocity_decay_steps * self.action_dt,
            max_velocity=self.config.max_velocity, gripper="hold")
