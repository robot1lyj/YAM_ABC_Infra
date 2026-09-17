"""Opt-in trained-RTC wire adapter; not connected to the motor control loop yet.

Thor owns the model and inverse transform. The controller will own the target
tick and committed physical actions when the execution contract is finalized.
"""

from __future__ import annotations

import numpy as np

from .policy import PlainPolicyClient


def build_rtc_request(
    observation: dict,
    *,
    target_start_tick: int,
    committed_actions,
    max_delay_steps: int,
) -> dict:
    """Build the strict condapi trained-RTC envelope without guessing a tick.

    The caller must supply already committed, absolute 14D controller targets;
    this function neither predicts them nor equates a camera time to a tick.
    """
    if not isinstance(observation, dict):
        raise ValueError("RTC observation must be a mapping")
    try:
        state = np.asarray(observation.get("observation.state"), dtype=np.float64)
    except (TypeError, ValueError):
        state = np.empty(0)
    if state.shape != (14,) or not np.isfinite(state).all():
        raise ValueError("RTC observation.state must be finite 14D")
    for role in ("top", "left", "right"):
        image = np.asarray(observation.get(f"observation.images.{role}_rgb"))
        if image.dtype != np.uint8 or image.ndim != 3 or image.shape[-1] != 3:
            raise ValueError(f"RTC {role} image must be HWC RGB uint8")
    if not isinstance(observation.get("prompt"), str) or not observation["prompt"].strip():
        raise ValueError("RTC prompt is required")
    if type(target_start_tick) is not int or target_start_tick < 0:
        raise ValueError("RTC target_start_tick must be a nonnegative control tick")
    if type(max_delay_steps) is not int or not 0 < max_delay_steps < 50:
        raise ValueError("RTC max_delay_steps must be 1–49")
    prefix = np.asarray(committed_actions, dtype=np.float32)
    if prefix.size == 0:
        prefix = prefix.reshape(0, 14)
    if prefix.ndim != 2 or prefix.shape[1] != 14 or not np.isfinite(prefix).all():
        raise ValueError("RTC committed_actions must be finite (d,14) absolute targets")
    delay = len(prefix)
    if delay > max_delay_steps:
        raise ValueError("RTC committed prefix exceeds trained delay range")
    return {
        "type": "infer",
        "obs": observation,
        "rtc": {
            "delay_steps": delay,
            "target_start_tick": target_start_tick,
            "committed_start_tick": target_start_tick,
            "committed_actions": prefix.copy(),
        },
    }


class RtcPolicyClient(PlainPolicyClient):
    """Strict RTC-only connection; never silently uses ordinary inference."""

    def __init__(self, url: str, timeout: float = 2.0):
        super().__init__(url, timeout=timeout)
        meta = self.metadata
        if (
            not isinstance(meta, dict)
            or meta.get("rtc_mode") != "trained"
            or type(meta.get("rtc_max_delay_steps")) is not int
            or not 0 < meta["rtc_max_delay_steps"] < 50
            or meta.get("action_horizon") != 50
            or meta.get("action_dim") != 14
        ):
            self.close()
            raise ValueError("Thor endpoint does not advertise trained RTC H50/14D")
        self.max_delay_steps = meta["rtc_max_delay_steps"]

    def infer_rtc(self, observation: dict, *, target_start_tick: int, committed_actions):
        payload = build_rtc_request(
            observation,
            target_start_tick=target_start_tick,
            committed_actions=committed_actions,
            max_delay_steps=self.max_delay_steps,
        )
        result = super().infer(payload)
        timing = result.get("server_timing") if isinstance(result, dict) else None
        if not isinstance(timing, dict) or timing.get("rtc_used") is not True:
            self.close()
            raise ValueError("Thor did not confirm trained RTC execution")
        actions = np.asarray(result.get("actions"), dtype=np.float64)
        prefix = payload["rtc"]["committed_actions"]
        if actions.shape != (50, 14) or not np.isfinite(actions).all():
            self.close()
            raise ValueError("Thor RTC response must be finite (50,14)")
        if not np.allclose(actions[:len(prefix)], prefix, rtol=0, atol=2e-6):
            self.close()
            raise ValueError("Thor RTC response changed committed actions")
        return result
