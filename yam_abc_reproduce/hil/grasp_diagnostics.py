"""Offline empty-grasp triage. Never changes actions or labels success automatically.

Explicit per-arm thresholds must come from empty-close/object-close recordings.
The optional runtime payload uses the *pre-command* SDK snapshot. Consequently
feedback is compared with the preceding submitted target, not a new same-row
command. Results are review candidates, not contact sensors or training labels.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

from .storage import read_rows


@dataclass(frozen=True)
class Thresholds:
    effort_nm: float
    empty_position: float
    position_tolerance: float = .03
    slow_velocity: float = .1
    closing_delta: float = .05
    confirm_s: float = .1
    max_feedback_age_s: float = .1
    max_gap_s: float = .1

    def __post_init__(self):
        if not all(math.isfinite(v) for v in asdict(self).values()):
            raise ValueError("thresholds must be finite")
        if not 0 <= self.empty_position <= 1:
            raise ValueError("empty position must be normalized [0,1]")
        if any(v <= 0 for k, v in asdict(self).items() if k != "empty_position"):
            raise ValueError("thresholds must be positive")


def location(row):
    """Both physical and compacted clocks; video indices are segment-local."""
    return {key: row.get(key) for key in (
        "tick", "time", "frame_index", "timestamp", "_segment", "video_indices",
        "epoch", "action_index", "policy_selection", "request",
        "episode_row", "elapsed_s", "policy_executed_step",
    )}


class Detector:
    def __init__(self, thresholds: Thresholds, arm: str):
        self.t = thresholds
        self.arm = arm
        self.previous = None
        self.active = None
        self.anchor = None
        self.pending = None
        self.last_feedback_stamp = None
        self.attempts = []

    def finish(self, reason):
        if self.active is not None:
            self.active["end_reason"] = reason
            self.attempts.append(self.active)
        self.active = self.pending = self.anchor = None
        self.last_feedback_stamp = None

    def observe(self, row, feedback, target):
        previous = self.previous
        self.previous = row
        now = row["time"]
        if previous is not None and (
            row.get("epoch") != previous.get("epoch") or row.get("wait_boundary")
            or not 0 < now - previous["time"] <= self.t.max_gap_s
        ):
            self.finish("timeline_boundary")
            previous = None
        if target is None or row.get("source") not in ("policy", "hold"):
            self.finish("non_policy")
            return
        if self.anchor is None:
            self.anchor = target
        if self.active is None:
            self.anchor = max(self.anchor, target)
            if row.get("source") == "policy" and self.anchor - target >= self.t.closing_delta:
                self.active = {"arm": self.arm, "command_start": location(row),
                               "minimum_target": target, "candidate": None,
                               "missing_feedback_frames": 0, "review_result": None}
        if self.active is None:
            return
        if target - self.active["minimum_target"] >= self.t.closing_delta:
            self.finish("reopened")
            self.anchor = target
            return
        self.active["minimum_target"] = min(self.active["minimum_target"], target)
        # The start-row feedback precedes this closure instruction.
        if previous is None or now <= self.active["command_start"]["time"]:
            return
        valid = isinstance(feedback, dict) and feedback.get("valid")
        keys = ("position", "velocity", "effort_nm", "sdk_updated_at", "sampled_at", "feedback_age_s")
        valid = valid and all(isinstance(feedback.get(k), (int, float))
                              and math.isfinite(feedback[k]) for k in keys)
        if valid:
            # Include IPC handoff/cache age; never accumulate repeated snapshots.
            age = feedback["feedback_age_s"] + max(0, now - feedback["sampled_at"])
            valid = 0 <= age <= self.t.max_feedback_age_s
            valid = valid and (self.last_feedback_stamp is None
                               or feedback["sdk_updated_at"] > self.last_feedback_stamp)
        if not valid:
            self.active["missing_feedback_frames"] += 1
            self.pending = None
            return
        self.last_feedback_stamp = feedback["sdk_updated_at"]
        if self.active["candidate"] is not None:
            return
        slow = abs(feedback["velocity"]) <= self.t.slow_velocity
        empty = feedback["position"] <= self.t.empty_position + self.t.position_tolerance
        effort = abs(feedback["effort_nm"]) >= self.t.effort_nm
        kind = "empty_close_candidate" if slow and empty else (
            "contact_candidate" if slow and effort else None)
        if kind is None:
            self.pending = None
            return
        if self.pending is None or self.pending["kind"] != kind:
            self.pending = {"kind": kind, "onset": location(row),
                            "preceding_command": location(previous),
                            "feedback_at_onset": dict(feedback)}
        if now - self.pending["onset"]["time"] >= self.t.confirm_s:
            self.active["candidate"] = dict(self.pending, confirmed=location(row))
            self.pending = None


def analyze(rows, *, left: Thresholds, right: Thresholds):
    detectors = [Detector(left, "left"), Detector(right, "right")]
    frames = diagnostic_frames = 0
    first_time = None
    policy_steps = 0
    for row in rows:
        if first_time is None:
            first_time = row["time"]
        policy_steps += int(row.get("source") == "policy")
        row = dict(row, episode_row=frames, elapsed_s=row["time"] - first_time,
                   policy_executed_step=policy_steps if row.get("source") == "policy" else None)
        frames += 1
        payload = row.get("grasp_diagnostics") or {}
        supported = payload.get("schema_version") == 1 and payload.get("sample_phase") == "before_command"
        diagnostic_frames += int(supported)
        feedback = payload.get("followers", []) if supported else []
        command = row.get("submitted_action")
        for i, detector in enumerate(detectors):
            target = float(command[6 + i * 7]) if command is not None else None
            if target is not None and not math.isfinite(target):
                target = None
            detector.observe(row, feedback[i] if len(feedback) > i else None, target)
    for detector in detectors:
        detector.finish("episode_end")
    return {"schema": "yam_grasp_review_v1", "frames": frames,
            "diagnostic_frames": diagnostic_frames,
            "thresholds": {"left": asdict(left), "right": asdict(right)},
            "note": "Candidates only; review video. No automatic grasp-success or drop labels.",
            "attempts": sorted(detectors[0].attempts + detectors[1].attempts,
                               key=lambda a: a["command_start"]["time"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episode", type=Path)
    parser.add_argument("--effort-nm", type=float, nargs=2, required=True, metavar=("LEFT", "RIGHT"))
    parser.add_argument("--empty-position", type=float, nargs=2, required=True, metavar=("LEFT", "RIGHT"))
    parser.add_argument("--output", type=Path, required=True, help="New review JSON; never overwrite raw data")
    args = parser.parse_args()
    thresholds = [Thresholds(e, p) for e, p in zip(args.effort_nm, args.empty_position, strict=True)]
    result = analyze(read_rows(args.episode), left=thresholds[0], right=thresholds[1])
    result["episode"] = str(args.episode.resolve())
    with args.output.open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write("\n")
    print(f"{result['diagnostic_frames']}/{result['frames']} diagnostic frames; "
          f"{len(result['attempts'])} closure attempts. Review: {args.output}")


if __name__ == "__main__":
    main()
