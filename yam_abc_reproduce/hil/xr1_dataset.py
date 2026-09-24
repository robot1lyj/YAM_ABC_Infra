"""Read-only HIL expert cleaning into XR-1 EEF training sidecars.

The source videos and HDF5 episode are never rewritten.  Each output segment
retains its camera frame indices and can yield native 30-step XR-1 windows.
Only contiguous, valid human-expert rows are admitted; policy/HOLD/wait rows
are not silently relabelled as demonstrations.
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .export import iter_expert_segments
from .kinematics import DualArmEefConverter
from .storage import ROLES, read_rows
from .xr1_actions import ACTION_DIM, ACTION_HORIZON, XR1YamCodec, action_mask


@dataclass(frozen=True)
class XR1ExpertSegment:
    """One uninterrupted expert run, with FK performed once per source row."""

    observation_state: np.ndarray  # (N,14), measured at the camera reference
    expert_action: np.ndarray  # (N,14), actually submitted human target
    observation_pose: np.ndarray  # (N,2,4,4), official grasp_site in each base
    action_pose: np.ndarray  # (N,2,4,4)
    observation_gripper: np.ndarray  # (N,2)
    action_gripper: np.ndarray  # (N,2)
    source_tick: np.ndarray  # (N,)
    source_time: np.ndarray  # (N,), host clock from the source episode
    source_video_index: np.ndarray  # (N,3), top/left/right
    source_segment: str

    def window(self, start: int) -> dict[str, np.ndarray]:
        """Return native 30x60 action with last-target padding and masks.

        The first action is paired with observation at the same source row.
        Never call across a segment boundary.  ``action_mask`` marks both
        valid timesteps and YAM-supported action dimensions.
        """
        if not 0 <= start < len(self.source_tick):
            raise IndexError("XR-1 window start outside expert segment")
        end = min(start + ACTION_HORIZON, len(self.source_tick))
        length = end - start
        raw = XR1YamCodec.encode_poses(
            self.observation_pose[start],
            self.observation_gripper[start],
            self.action_pose[start:end],
            self.action_gripper[start:end],
        )
        padded = np.empty((ACTION_HORIZON, ACTION_DIM), dtype=np.float32)
        padded[:length] = raw
        padded[length:] = raw[-1]  # same padding rule as Xiaomi JSONDataset._pad
        return {
            "state": XR1YamCodec.state60(self.observation_state[start]),
            "action": padded,
            "action_mask": action_mask(length),
            "step_mask": np.arange(ACTION_HORIZON) < length,
            "source_tick": self.source_tick[start:end].copy(),
            "source_time": self.source_time[start:end].copy(),
            "source_video_index": self.source_video_index[start].copy(),
        }


def clean_expert_rows(rows, kinematics: DualArmEefConverter) -> XR1ExpertSegment:
    """Convert rows already selected by ``iter_expert_segments``."""
    if not rows:
        raise ValueError("empty expert segment")
    state = np.asarray([row["observation_state"] for row in rows], dtype=np.float64)
    action = np.asarray([row["submitted_action"] for row in rows], dtype=np.float64)
    if state.shape != (len(rows), 14) or action.shape != state.shape:
        raise ValueError("expert state/action must be (N,14)")
    if not np.isfinite(state).all() or not np.isfinite(action).all():
        raise ValueError("expert state/action must be finite")
    times = np.asarray([row["time"] for row in rows], dtype=np.float64)
    if not np.isfinite(times).all() or np.any(np.diff(times) <= 0):
        raise ValueError("expert source times must be finite and increasing")
    observation_pose, observation_gripper = kinematics.forward_batch(state)
    action_pose, action_gripper = kinematics.forward_batch(action)
    video = np.asarray(
        [[row["video_indices"][role] for role in ROLES] for row in rows], dtype=np.int64
    )
    return XR1ExpertSegment(
        state,
        action,
        observation_pose,
        action_pose,
        observation_gripper,
        action_gripper,
        np.asarray([row["tick"] for row in rows], dtype=np.int64),
        times,
        video,
        str(rows[0].get("_segment", "")),
    )


def iter_cleaned_expert_segments(episode: Path):
    """Stream cleaned runs without changing source data or touching motors."""
    source = Path(episode)
    manifest = json.loads((source / "manifest.json").read_text())
    if manifest.get("schema") not in ("yam_hil_v1", "yam_hil_v2"):
        raise ValueError("not a YAM HIL episode")
    if manifest.get("error") or manifest.get("outcome") in ("aborted", "discarded"):
        raise ValueError("aborted/discarded episode requires review before expert export")
    if not np.isclose(float(manifest["fps"]), 30.0):
        raise ValueError("XR-1 cleaning requires a 30 Hz source episode")
    kinematics = DualArmEefConverter()
    for rows in iter_expert_segments(read_rows(source)):
        yield clean_expert_rows(rows, kinematics)


def export_xr1_sidecar(episode: Path, output: Path) -> int:
    """Write numeric FK/IK-ready sidecars; source videos remain authoritative.

    ``output`` must be new.  A temporary sibling is renamed only after every
    expert run succeeds, so an IK/FK/IO failure cannot leave a finished-looking
    partial export.  No images are copied or silently time-compressed here.
    """
    source, destination = Path(episode).resolve(), Path(output).resolve()
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    entries = []
    try:
        for index, segment in enumerate(iter_cleaned_expert_segments(source)):
            filename = f"expert_{index:06d}.npz"
            np.savez_compressed(
                temporary / filename,
                observation_state=segment.observation_state,
                expert_action=segment.expert_action,
                observation_pose=segment.observation_pose,
                action_pose=segment.action_pose,
                observation_gripper=segment.observation_gripper,
                action_gripper=segment.action_gripper,
                source_tick=segment.source_tick,
                source_time=segment.source_time,
                source_video_index=segment.source_video_index,
            )
            entries.append(
                {"file": filename, "rows": len(segment.source_tick), "source_segment": segment.source_segment}
            )
        (temporary / "manifest.json").write_text(
            json.dumps(
                {
                    "schema": "yam_xr1_eef_sidecar_v1",
                    "source_episode": str(source),
                    "action_horizon": ACTION_HORIZON,
                    "fps": 30,
                    "camera_roles": list(ROLES),
                    "expert_only": True,
                    "coordinate_frame": "each_arm_base/linear_4310/grasp_site",
                    "segments": entries,
                },
                indent=2,
            ) + "\n"
        )
        temporary.rename(destination)
    except Exception:
        shutil.rmtree(temporary)
        raise
    return len(entries)


def iter_sidecar_segments(output: Path):
    """Load a numeric export for training-window generation, without pickle."""
    folder = Path(output)
    manifest = json.loads((folder / "manifest.json").read_text())
    if manifest.get("schema") != "yam_xr1_eef_sidecar_v1":
        raise ValueError("not a YAM XR-1 EEF sidecar")
    for entry in manifest["segments"]:
        path = folder / entry["file"]
        if not path.resolve().is_relative_to(folder.resolve()):
            raise ValueError("XR-1 sidecar path escapes export directory")
        with np.load(path, allow_pickle=False) as data:
            yield XR1ExpertSegment(
                *(data[name].copy() for name in (
                    "observation_state", "expert_action", "observation_pose",
                    "action_pose", "observation_gripper", "action_gripper",
                    "source_tick", "source_time", "source_video_index",
                )),
                entry["source_segment"],
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episode", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(f"Exported {export_xr1_sidecar(args.episode, args.output)} XR-1 expert segments")


if __name__ == "__main__":
    main()
