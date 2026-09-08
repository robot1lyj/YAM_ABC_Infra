"""Export contiguous expert segments to the existing canonical episode format.

python -m yam_abc_reproduce.hil.export EPISODE --output DATASET
Policy and hold rows are excluded. Each human segment becomes its own episode,
so training chunks cannot cross a policy interval hidden by filtering.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from .. import __version__
from ..data.schema import WRITE_COMPLETE_FLAG, CameraMeta, EpisodeMeta


def _segments(rows):
    current = []
    for row in rows:
        valid = (
            row.get("expert_valid")
            and row.get("source") == "human"
            and row.get("observation_state") is not None
            and set(row.get("video_indices", {})) == {"top", "left", "right"}
        )
        if current and (
            not valid
            or row["tick"] != current[-1]["tick"] + 1
            or row["epoch"] != current[-1]["epoch"]
        ):
            yield current
            current = []
        if valid:
            current.append(row)
    if current:
        yield current


def _video_subset(source, destination, indices, fps):
    import av

    indices = list(indices)
    if indices != sorted(set(indices)):
        raise ValueError("video indices must be unique and increasing")
    wanted = iter(indices)
    next_index = next(wanted, None)
    width = height = 0
    with av.open(str(source)) as inp, av.open(str(destination), "w") as out:
        stream = None
        for i, frame in enumerate(inp.decode(video=0)):
            if next_index is None:
                break
            if i != next_index:
                continue
            if stream is None:
                width, height = frame.width, frame.height
                stream = out.add_stream(
                    "libx264", rate=int(fps), options={"crf": "18", "preset": "fast"}
                )
                stream.width, stream.height = width, height
                stream.pix_fmt = "yuv420p"
            frame.pts = None
            for packet in stream.encode(frame):
                out.mux(packet)
            next_index = next(wanted, None)
        if next_index is not None:
            raise ValueError("video truncated before a selected expert frame")
        if stream:
            for packet in stream.encode():
                out.mux(packet)
    return width, height


def export(source: Path, output: Path):
    manifest = json.loads((source / "manifest.json").read_text())
    if manifest.get("schema") != "yam_hil_v1":
        raise ValueError("not a YAM HIL episode")
    if manifest.get("error") or manifest.get("outcome") in ("aborted", "discarded"):
        raise ValueError("aborted recording requires review before expert export")
    output.mkdir(parents=True, exist_ok=False)
    fps = manifest["fps"]
    total = 0
    with (source / "steps.jsonl").open() as log:
        for index, rows in enumerate(_segments(json.loads(line) for line in log)):
            dst = output / f"episode_{index:06d}"
            dst.mkdir()
            state = np.array([r["observation_state"] for r in rows])
            action = np.array([r["submitted_action"] for r in rows])
            if state.shape != (len(rows), 14) or action.shape != state.shape:
                raise ValueError("invalid expert state/action shape")
            for i, arm in enumerate(("left", "right")):
                offset = i * 7
                np.save(dst / f"{arm}-joint_pos.npy", state[:, offset : offset + 6])
                np.save(dst / f"{arm}-gripper_pos.npy", state[:, offset + 6 : offset + 7])
                np.save(dst / f"action-{arm}-joint.npy", action[:, offset : offset + 6])
                np.save(dst / f"action-{arm}-gripper.npy", action[:, offset + 6 : offset + 7])
            cameras = []
            for role in ("top", "left", "right"):
                width, height = _video_subset(
                    source / f"{role}.mp4",
                    dst / f"{role}-images-rgb.mp4",
                    [r["video_indices"][role] for r in rows],
                    fps,
                )
                times = np.array(
                    [r["sync"]["cameras"][role]["host_received_at"] * 1000 for r in rows]
                )
                np.save(dst / f"{role}-timestamp.npy", times)
                cameras.append(
                    CameraMeta(
                        role,
                        "realsense" if not manifest["mock"] else "mock",
                        role,
                        "mono",
                        ["rgb"],
                        width,
                        height,
                        int(fps),
                    )
                )
            metadata = EpisodeMeta(
                __version__,
                1,
                datetime.now(UTC).isoformat(),
                manifest["station"]["task_name"],
                ["left", "right"],
                6,
                fps,
                cameras=cameras,
                num_frames=len(rows),
                extra={
                    "source_hil_episode": str(source.resolve()),
                    "source_ticks": [r["tick"] for r in rows],
                    "expert_only": True,
                    "mock": manifest["mock"],
                    "source_outcome": manifest["outcome"],
                    "timestamp_domain": "host_monotonic_ms",
                    "state_alignment": "camera arrival reference; interpolated state",
                },
            )
            metadata.to_json(dst / "metadata.json")
            # Retain all intervention/timing provenance through conversion input.
            (dst / "hil_provenance.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
            (dst / WRITE_COMPLETE_FLAG).touch()
            total += 1
    return total


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("episode", type=Path)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    print(f"Exported {export(args.episode, args.output)} expert segments")


if __name__ == "__main__":
    main()
