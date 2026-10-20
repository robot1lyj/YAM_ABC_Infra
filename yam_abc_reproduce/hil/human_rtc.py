"""Human-motion guidance for shadow inference, separate from physical RTC commitments.

The control owner supplies successful SDK submissions. Predicted human motion and
shadow replies can guide a fresh handover RPC; neither can directly move a robot.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass

import numpy as np

SCHEMA = "yam_human_rtc_v1"
SETTINGS = frozenset({"enable_human_rtc", "human_rtc_prefix_attention_horizon",
    "human_rtc_velocity_decay_steps", "human_rtc_max_velocity",
    "human_rtc_max_gripper_velocity", "human_rtc_max_reference_age"})


@dataclass(frozen=True)
class HumanRtcConfig:
    enabled: bool = True
    prefix_attention_horizon: int = 20
    velocity_decay_steps: int = 20
    max_velocity: float = 2.0  # rad/s, arm joints only
    max_gripper_velocity: float = 2.0  # normalized opening/s
    max_reference_age: float = 0.5

    @classmethod
    def from_settings(cls, settings):
        cfg = cls(**{("enabled" if key == "enable_human_rtc" else key.removeprefix("human_rtc_")): value
                     for key, value in settings.items() if key in SETTINGS})
        if type(cfg.enabled) is not bool:
            raise ValueError("enable_human_rtc must be true or false")
        for name in ("prefix_attention_horizon", "velocity_decay_steps"):
            if type(getattr(cfg, name)) is not int or not 1 <= getattr(cfg, name) < 50:
                raise ValueError(f"human_rtc_{name} must be an integer from 1 to 49")
        for name in ("max_velocity", "max_gripper_velocity", "max_reference_age"):
            value = getattr(cfg, name)
            if type(value) not in (float, int) or not np.isfinite(value) or value <= 0:
                raise ValueError(f"human_rtc_{name} must be finite and positive")
        return cfg


def build_human_prefix(latest, previous, *, sample_dt, action_dt, config, limit_target):
    """VAM's cubic velocity decay, in the checkpoint's original action timebase."""
    latest, previous = np.asarray(latest, dtype=float), np.asarray(previous, dtype=float)
    if (latest.shape != (14,) or previous.shape != (14,)
            or not np.isfinite(latest).all() or not np.isfinite(previous).all()
            or not np.isfinite(sample_dt) or sample_dt <= 0
            or not np.isfinite(action_dt) or action_dt <= 0):
        raise ValueError("invalid human command samples/timebase")
    bounds = np.full(14, config.max_velocity)
    bounds[[6, 13]] = config.max_gripper_velocity
    velocity = np.clip((latest - previous) / sample_dt, -bounds, bounds)
    actions = np.empty((50, 14))
    actions[0] = limit_target(latest.copy())
    for step in range(1, 50):
        u = min(step / config.velocity_decay_steps, 1.0)
        scale = 1 - (3 * u**2 - 2 * u**3)
        actions[step] = limit_target(actions[step - 1] + velocity * scale * action_dt)
    return actions


def validate_guidance(guidance, *, metadata=None):
    """Bound the new wire field without ever treating its reference as committed."""
    if not isinstance(guidance, dict) or guidance.get("schema") != SCHEMA:
        raise ValueError("invalid human RTC schema")
    if guidance.get("mode") not in ("shadow", "handover"):
        raise ValueError("invalid human RTC mode")
    for key in ("epoch", "request_id", "observation_id", "observation_policy_tick", "source_tick", "reference_start_tick"):
        if type(guidance.get(key)) is not int or guidance[key] < 0:
            raise ValueError(f"invalid human RTC {key}")
    if not np.isclose(guidance.get("action_dt_s", 0), 1 / 30, rtol=0, atol=1e-6):
        raise ValueError("human RTC requires the 30 Hz action timebase")
    for key in ("prefix_attention_horizon", "velocity_decay_steps"):
        if type(guidance.get(key)) is not int or not 1 <= guidance[key] < 50:
            raise ValueError(f"invalid human RTC {key}")
    rows = np.asarray(guidance.get("reference_actions"), dtype=float)
    if rows.shape != (50, 14) or not np.isfinite(rows).all():
        raise ValueError("human RTC reference must be finite (50,14)")
    if np.any((rows[:, [6, 13]] < 0) | (rows[:, [6, 13]] > 1)):
        raise ValueError("human RTC gripper reference outside [0,1]")
    if metadata is not None:
        cap = require_capability(metadata)
        for key in ("prefix_attention_horizon", "velocity_decay_steps"):
            if guidance[key] > cap["max_" + key]:
                raise ValueError("human RTC request exceeds advertised capability")


def require_capability(metadata, config=None):
    cap = metadata.get("human_rtc") if isinstance(metadata, dict) else None
    if (not isinstance(cap, dict) or cap.get("schema") != SCHEMA
            or not isinstance(cap.get("modes"), list)
            or not {"shadow", "handover"}.issubset(cap["modes"])
            or any(type(cap.get(key)) is not int or not 1 <= cap[key] < 50
                   for key in ("max_prefix_attention_horizon", "max_velocity_decay_steps"))):
        raise ValueError("Thor未声明Human RTC支持；保持原有交还流程")
    if config and (cap.get("max_prefix_attention_horizon", 0) < config.prefix_attention_horizon
                   or cap.get("max_velocity_decay_steps", 0) < config.velocity_decay_steps):
        raise ValueError("Thor Human RTC支持范围小于本站配置")
    return cap


def validate_confirmation(request, response):
    confirmation = response.get("human_rtc")
    keys = ("schema", "mode", "epoch", "request_id", "observation_id",
            "observation_policy_tick", "source_tick", "reference_start_tick")
    if (not isinstance(confirmation, dict)
            or any(type(confirmation.get(key)) is not type(request[key])
                   or confirmation[key] != request[key] for key in keys)
            or (response.get("server_timing") or {}).get("human_rtc_used") is not True):
        raise ValueError("Thor did not confirm Human RTC request identity/use")


class HumanRtc:
    """Tick-owned samples/cache. Only an explicit handover exports a cached reference."""

    def __init__(self, config, *, action_dt=1 / 30):
        self.config, self.action_dt = config, action_dt
        self.samples = deque(maxlen=2)
        self.epoch = None
        self.locked_epoch = None
        self.pending = None
        self.cached = None
        self.handover = None
        self.last_request_at = -float("inf")
        self.reason = "等待服务握手"
        self.active = False
        self.accepted = 0

    def clear(self):
        self.samples.clear()
        self.pending = self.cached = self.handover = None
        self.locked_epoch = None
        self.last_request_at = -float("inf")

    def transition(self, arbiter, event, now):
        candidate = None
        if (event == "resume_policy" and self.cached is not None and self.active
                and self.epoch == arbiter.epoch and self.cached[0].epoch == arbiter.epoch):
            if 0 <= now - self.cached[0].created_at <= self.config.max_reference_age:
                candidate = self.cached
        if event is not None or self.epoch != arbiter.epoch:
            self.clear()
            self.epoch = arbiter.epoch
        return candidate

    def after_transition(self, arbiter, event, candidate):
        self.epoch = arbiter.epoch
        if event == "resume_policy":
            self.handover = candidate
        elif event == "handback_hold":
            self.locked_epoch = arbiter.epoch

    def refresh(self, arbiter, metadata, *, leaders_connected=True, pld=False, ready=True):
        self.active = False
        if not self.config.enabled:
            self.reason = "配置已关闭"
        elif (arbiter.mode != "hil" or arbiter.hil_input != "leader"
              or arbiter.rtc_timeline is None or not leaders_connected or pld):
            self.reason = "仅支持Leader HIL、原生RTC、PLD关闭"
        elif not ready:
            self.reason = "推理通信尚未就绪"
        else:
            try:
                require_capability(metadata, self.config)
                self.active, self.reason = True, None
            except (ValueError, TypeError):
                self.reason = "Thor未声明兼容的Human RTC能力；使用原有交还流程"
        if not self.active:
            self.clear()

    def background_allowed(self, arbiter):
        return self.active and (arbiter.phase == "human" or
            (arbiter.phase == "hold" and arbiter.intervention_pending
             and self.locked_epoch == arbiter.epoch))

    def record_submitted(self, arbiter, action, *, tick, now):
        if not self.background_allowed(arbiter):
            self.samples.clear()
            return
        if self.samples and (tick != self.samples[-1][0] + 1
                             or not 0 < now - self.samples[-1][1] <= self.config.max_reference_age):
            self.samples.clear()
        self.samples.append((tick, now, np.asarray(action).copy()))

    def shadow_request(self, arbiter, token, *, tick, now, limit_target):
        if (not self.background_allowed(arbiter) or self.pending is not None or len(self.samples) < 2
                or now - self.last_request_at < arbiter.replan_period):
            return None
        old, new = self.samples
        reference = build_human_prefix(new[2], old[2], sample_dt=new[1] - old[1],
            action_dt=self.action_dt, config=self.config, limit_target=limit_target)
        return self._guidance(token, reference, mode="shadow", source_tick=tick,
            source="submitted_human_commands", source_request=None, source_model_index=0)

    def _guidance(self, token, reference, *, mode, source_tick, source, source_request, source_model_index):
        return dict(schema=SCHEMA, mode=mode, epoch=token.epoch, request_id=token.request_id,
            observation_id=token.observation_id, observation_policy_tick=token.observation_policy_tick,
            source_tick=source_tick, reference_actions=reference.copy(), action_dt_s=self.action_dt,
            reference_start_tick=source_tick if mode == "shadow" else token.observation_policy_tick,
            prefix_attention_horizon=self.config.prefix_attention_horizon,
            velocity_decay_steps=self.config.velocity_decay_steps, reference_source=source,
            source_request=source_request, source_model_index=source_model_index)

    def accept(self, arbiter, reply, *, now):
        match = reply.token == self.pending
        if match:
            self.pending = None
        accepted = (match and self.background_allowed(arbiter) and reply.token.epoch == arbiter.epoch
                    and 0 <= now - reply.token.created_at <= self.config.max_reference_age
                    and reply.error is None)
        if accepted:
            self.cached = (reply.token, reply.actions.copy())
            self.accepted += 1
        return accepted

    def handover_request(self, token, *, now, limit_target):
        if not self.active or self.handover is None:
            return None
        source, actions = self.handover
        if not 0 <= now - source.created_at <= self.config.max_reference_age:
            self.handover = None
            return None
        offset = token.observation_policy_tick - source.observation_policy_tick
        if not 0 <= offset < len(actions):
            self.handover = None
            return None
        reference = np.array([limit_target(actions[min(offset + i, 49)].copy()) for i in range(50)])
        return self._guidance(token, reference, mode="handover", source_tick=source.observation_policy_tick,
            source="shadow_model", source_request=asdict(source), source_model_index=offset)

    def snapshot(self):
        return dict(configured=self.config.enabled, available=self.active, reason=self.reason,
            pending=self.pending is not None, accepted_replies=self.accepted,
            prefix_attention_horizon=self.config.prefix_attention_horizon,
            velocity_decay_steps=self.config.velocity_decay_steps,
            velocity_decay_seconds=self.config.velocity_decay_steps * self.action_dt)
