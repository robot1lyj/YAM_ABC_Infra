"""Strict trained-RTC wire adapter for the 30 Hz controller timeline.

Thor owns the model and inverse transform. The controller will own the target
    tick and committed physical actions; the server never guesses either.
"""

from __future__ import annotations

import numpy as np

from .policy import PlainPolicyClient, PolicyResponseError


def build_rtc_request(
    observation: dict,
    *,
    target_start_tick: int,
    committed_actions,
    max_delay_steps: int,
    pld: dict | None = None,
    human_rtc: dict | None = None,
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
    payload = {
        "type": "infer",
        "obs": observation,
        "rtc": {
            "delay_steps": delay,
            "observation_policy_tick": target_start_tick,
            "target_start_tick": target_start_tick,
            "committed_start_tick": target_start_tick,
            "committed_actions": prefix.copy(),
        },
    }
    if pld is not None:
        payload["pld"] = pld
    if human_rtc is not None:
        from .human_rtc import validate_guidance
        validate_guidance(human_rtc)
        if human_rtc["mode"] != "handover" or human_rtc["observation_policy_tick"] != target_start_tick:
            raise ValueError("human RTC handover must match committed observation tick")
        if pld is not None:
            raise ValueError("Human RTC cannot be combined with PLD")
        import copy
        payload["human_rtc"] = copy.deepcopy(human_rtc)
    return payload


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
            or not isinstance(meta.get("action_dt_s"), (int, float))
            or not np.isclose(meta["action_dt_s"], 1 / 30, rtol=0, atol=1e-6)
        ):
            self.close()
            raise ValueError("Thor endpoint does not advertise trained RTC H50/14D")
        self.max_delay_steps = meta["rtc_max_delay_steps"]

    def infer_human_rtc(self, observation, *, human_rtc):
        from .human_rtc import require_capability, validate_confirmation, validate_guidance
        require_capability(self.metadata)
        validate_guidance(human_rtc, metadata=self.metadata)
        if human_rtc["mode"] != "shadow":
            raise ValueError("shadow inference requires shadow guidance")
        payload = build_rtc_request(observation, target_start_tick=human_rtc["observation_policy_tick"],
            committed_actions=[], max_delay_steps=self.max_delay_steps)
        payload.pop("rtc")  # Human futures are guidance, never a physical commitment.
        payload["human_rtc"] = human_rtc
        result = super().infer(payload)
        try:
            validate_confirmation(human_rtc, result)
            actions = np.asarray(result.get("actions"), dtype=float)
            if actions.shape != (50, 14) or not np.isfinite(actions).all():
                raise ValueError("shadow actions must be finite (50,14)")
        except (ValueError, TypeError) as exc:
            self.close()
            raise PolicyResponseError(str(exc), result) from exc
        return result

    def infer_rtc(self, observation: dict, *, target_start_tick: int, committed_actions, pld=None, human_rtc=None):
        if human_rtc is not None:
            from .human_rtc import validate_guidance
            validate_guidance(human_rtc, metadata=self.metadata)
        payload = build_rtc_request(
            observation,
            target_start_tick=target_start_tick,
            committed_actions=committed_actions,
            max_delay_steps=self.max_delay_steps,
            pld=pld,
            human_rtc=human_rtc,
        )
        result = super().infer(payload)
        timing = result.get("server_timing") if isinstance(result, dict) else None
        if not isinstance(timing, dict) or timing.get("rtc_used") is not True:
            self.close()
            raise PolicyResponseError("Thor did not confirm trained RTC execution", result)
        actions = np.asarray(result.get("actions"), dtype=np.float64)
        prefix = payload["rtc"]["committed_actions"]
        if actions.shape != (50, 14) or not np.isfinite(actions).all():
            self.close()
            raise PolicyResponseError("Thor RTC response must be finite (50,14)", result)
        if not np.allclose(actions[:len(prefix)], prefix, rtol=0, atol=2e-6):
            self.close()
            raise PolicyResponseError("Thor RTC response changed committed actions", result)
        if human_rtc is not None:
            from .human_rtc import validate_confirmation
            try:
                validate_confirmation(human_rtc, result)
            except (ValueError, TypeError) as exc:
                self.close()
                raise PolicyResponseError(str(exc), result) from exc
        return result
