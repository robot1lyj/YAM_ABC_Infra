"""Run-locked client parameters; unknown research parameters stay unset."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from functools import cached_property

ARMS = ("left", "right")
INDICES = {"left": tuple(range(6)), "right": tuple(range(7, 13))}
PROTOCOL = "yam-parts-v1"
SCHEMA = "yam_parts_raw_v1"
RULES_SCHEMA = "rules_auto_v2"


@dataclass(frozen=True)
class ArmConfig:
    h_entry_m: float = 0.05
    h_goal_m: float | None = None
    minimum_height_m: float | None = None
    budget_s: float | None = None
    entry_hysteresis_m: float = 0.0
    minimum_descent_m_s: float = 0.0
    table: dict | None = None
    B_rad: tuple | None = None
    open_position_min: float | None = 0.8
    open_confirm_s: float | None = 0.3
    open_confirm_samples: int = 10


@dataclass(frozen=True)
class PartsConfig:
    mode: str = "off"
    contract_sha: str | None = None
    behavior_snapshot_id: str | None = None
    behavior_manifest_ref: str | None = None
    feature_schema_id: str | None = None
    reward: dict | None = None
    confirm_s: float | None = None
    max_feedback_age_s: float | None = None
    max_pose_age_s: float | None = None
    max_confirmation_gap_s: float | None = None
    continuity_rad: float | None = None
    close_delta: float | None = None
    release_position: float | None = None
    selector: dict = field(default_factory=lambda: {"schema": RULES_SCHEMA})
    left: ArmConfig = field(default_factory=ArmConfig)
    right: ArmConfig = field(default_factory=ArmConfig)

    @classmethod
    def from_dict(cls, value=None):
        # Own nested calibration/rule data; callers cannot mutate a run's
        # parameters after its configuration hash has been computed.
        data = copy.deepcopy(value or {})
        for arm in ARMS:
            if arm in data:
                data[arm] = ArmConfig(**data[arm])
        config = cls(**data)
        if config.selector != {"schema": RULES_SCHEMA}:
            raise ValueError(f"PARTS selector must be {RULES_SCHEMA}")
        if config.mode not in ("off", "shadow", "collect", "eval"):
            raise ValueError("PARTS mode must be off/shadow/collect/eval")
        for key in (
            "confirm_s",
            "max_feedback_age_s",
            "max_pose_age_s",
            "max_confirmation_gap_s",
            "continuity_rad",
            "close_delta",
            "release_position",
        ):
            number = getattr(config, key)
            if number is not None and (
                type(number) not in (int, float) or not math.isfinite(number) or number < 0
            ):
                raise ValueError(f"PARTS {key} must be finite and nonnegative")
            if (
                key
                in (
                    "max_feedback_age_s",
                    "max_pose_age_s",
                    "max_confirmation_gap_s",
                    "continuity_rad",
                )
                and number == 0
            ):
                raise ValueError(f"PARTS {key} must be positive when configured")
        if config.close_delta is not None and not 0 < config.close_delta <= 1:
            raise ValueError("PARTS close_delta must be in (0,1]")
        if config.release_position is not None and config.release_position > 1:
            raise ValueError("release_position must be in [0,1]")
        for arm in ARMS:
            options = getattr(config, arm)
            for key in (
                "h_entry_m",
                "h_goal_m",
                "minimum_height_m",
                "budget_s",
                "entry_hysteresis_m",
                "minimum_descent_m_s",
                "open_position_min",
                "open_confirm_s",
            ):
                number = getattr(options, key)
                if number is not None and (
                    type(number) not in (int, float) or not math.isfinite(number)
                ):
                    raise ValueError(f"PARTS {arm}.{key} must be finite")
            if options.h_entry_m is None:
                raise ValueError("PARTS entry height must be specified")
            if options.table is not None:
                from .height import validate_table

                validate_table(options.table, arm)
            if options.entry_hysteresis_m < 0 or options.minimum_descent_m_s < 0:
                raise ValueError("PARTS entry rules must be nonnegative")
            if options.budget_s is not None and options.budget_s <= 0:
                raise ValueError("PARTS budget_s must be positive")
            if options.open_position_min is not None and not 0 < options.open_position_min <= 1:
                raise ValueError("PARTS actual open threshold must be in (0,1]")
            if options.open_confirm_s is not None and options.open_confirm_s <= 0:
                raise ValueError("PARTS open confirmation must be positive")
            if type(options.open_confirm_samples) is not int or options.open_confirm_samples <= 0:
                raise ValueError("PARTS open_confirm_samples must be a positive integer")
            if options.B_rad is not None and (
                len(options.B_rad) != 6
                or any(
                    type(b) not in (int, float) or not math.isfinite(b) or b < 0
                    for b in options.B_rad
                )
            ):
                raise ValueError("PARTS B_rad must contain six finite nonnegative radians")
        if config.reward is not None:
            from .reward import components

            components(config, error_m=0)  # Fail at configuration, not in a control tick.
        return config

    def gaps(self):
        missing = [
            key
            for key in (
                "contract_sha",
                "behavior_snapshot_id",
                "behavior_manifest_ref",
                "feature_schema_id",
                "reward",
                "confirm_s",
                "max_feedback_age_s",
                "max_pose_age_s",
                "max_confirmation_gap_s",
                "continuity_rad",
                "close_delta",
                "release_position",
            )
            if getattr(self, key) is None
        ]
        for arm in ARMS:
            for key in ("h_goal_m", "minimum_height_m", "budget_s", "table", "B_rad"):
                if getattr(getattr(self, arm), key) is None:
                    missing.append(f"{arm}.{key}")
            missing.extend(
                f"{arm}.{key}"
                for key in self.selector_gaps(arm)
                if not hasattr(self, key) and f"{arm}.{key}" not in missing
            )
        return missing

    def selector_gaps(self, arm):
        fields = (
            "table",
            "open_position_min",
            "open_confirm_s",
        )
        return [key for key in fields if getattr(getattr(self, arm), key) is None] + [
            key
            for key in (
                "close_delta",
                "release_position",
                "confirm_s",
                "max_pose_age_s",
                "max_feedback_age_s",
                "max_confirmation_gap_s",
            )
            if getattr(self, key) is None
        ]

    @cached_property
    def selector_config_sha(self):
        """Hash actual rule parameters, independent of actor/model identity."""
        body = dict(
            self.selector,
            force_threshold_nm=0.65,
            **{
                key: getattr(self, key)
                for key in (
                    "confirm_s",
                    "max_pose_age_s",
                    "max_feedback_age_s",
                    "max_confirmation_gap_s",
                    "close_delta",
                    "release_position",
                )
            },
        )
        for arm in ARMS:
            body[arm] = {
                key: getattr(getattr(self, arm), key)
                for key in (
                    "table",
                    "open_position_min",
                    "open_confirm_s",
                    "open_confirm_samples",
                    "h_entry_m",
                    "entry_hysteresis_m",
                    "minimum_descent_m_s",
                )
            }
        return hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()

    def validate_execution(self):
        if self.mode in ("collect", "eval") and self.gaps():
            raise ValueError("PARTS parameters unset: " + ", ".join(self.gaps()))

    def as_dict(self):
        return asdict(self)
