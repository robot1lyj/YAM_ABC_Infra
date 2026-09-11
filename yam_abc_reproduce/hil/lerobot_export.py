"""Stream completed workstation recordings into the LeRobot v3.0 on-disk schema.

No torch/model stack is needed on RK3588. PyArrow writes tables; PyAV encodes
RGB video. The producer's JSONL remains the full timing/intervention provenance.
The format contract is checked against the pinned official LeRobot reader.
"""

from __future__ import annotations

import argparse
import itertools
import json
from contextlib import ExitStack
from pathlib import Path

import av
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .core import vector
from .storage import digest, episode_segments, read_rows
from .video_copy import remux_segments

ROLES = ("top", "left", "right")
NAMES = [
    name
    for arm in ("left", "right")
    for name in [*[f"{arm}_joint_{i}" for i in range(6)], f"{arm}_gripper"]
]
DATA_PATH = "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet"
VIDEO_PATH = "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4"


def _json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


class Moments:
    def __init__(self):
        self.count = 0

    def add(self, values):
        values = np.asarray(values, dtype=np.float64)
        n = len(values)
        mean, var = values.mean(axis=0), values.var(axis=0)
        low, high = values.min(axis=0), values.max(axis=0)
        self.merge(n, mean, var, low, high)

    def merge(self, n, mean, var, low, high):
        if not self.count:
            self.mean, self.m2, self.low, self.high = mean.copy(), var * n, low.copy(), high.copy()
        else:
            delta = mean - self.mean
            self.m2 += var * n + delta**2 * self.count * n / (self.count + n)
            self.mean += delta * n / (self.count + n)
            self.low, self.high = np.minimum(self.low, low), np.maximum(self.high, high)
        self.count += n

    def result(self, *, image_frames=None):
        def shape(value):
            return (value.reshape(3, 1, 1) if image_frames is not None else value).tolist()

        return {
            "min": shape(self.low),
            "max": shape(self.high),
            "mean": shape(self.mean),
            "std": shape(np.sqrt(np.maximum(0, self.m2 / self.count))),
            "count": [self.count if image_frames is None else image_frames],
        }


def _groups(path):
    """Preserve temporal gaps as boundaries, never silently stitch removed rows."""
    segment, previous = 0, None
    for row in read_rows(path):
        valid = row.get("source") in ("human", "policy", "hold") and set(
            row.get("video_indices", {})
        ) == set(ROLES)
        if not valid:
            previous = None
            segment += 1
            continue
        if previous is not None and (row["tick"] != previous[0] + 1):
            segment += 1
        previous = row["tick"], row["epoch"]
        yield segment, row


def expert_groups(path):
    segment, previous = 0, None
    for row in read_rows(path):
        valid = (
            row.get("source") == "human"
            and row.get("expert_valid")
            and row.get("observation_valid")
            and row.get("observation_state") is not None
            and set(row.get("video_indices", {})) == set(ROLES)
        )
        if not valid:
            previous = None
            segment += 1
            continue
        if previous is not None and (row["tick"] != previous[0] + 1 or row["epoch"] != previous[1]):
            segment += 1
        previous = row["tick"], row["epoch"]
        yield segment, row


class DatasetWriter:
    def __init__(self, root, fps, task):
        self.root, self.fps, self.task = Path(root), int(fps), task
        self.features = {
            "observation.state": {"dtype": "float32", "shape": [14], "names": NAMES},
            "action": {"dtype": "float32", "shape": [14], "names": NAMES},
            "complementary_info.measured_state": {
                "dtype": "float32",
                "shape": [14],
                "names": NAMES,
            },
        }
        for key, dtype in {
            "timestamp": "float32",
            "frame_index": "int64",
            "episode_index": "int64",
            "index": "int64",
            "task_index": "int64",
            "complementary_info.is_intervention": "bool",
            "complementary_info.action_source": "int64",
            "complementary_info.event": "int64",
            "complementary_info.intervention_id": "int64",
            "complementary_info.source_tick": "int64",
            "complementary_info.observation_valid": "bool",
            "complementary_info.expert_valid": "bool",
            "complementary_info.control_time": "float64",
            "complementary_info.event_requested_at": "float64",
            "complementary_info.event_applied_at": "float64",
        }.items():
            self.features[key] = {"dtype": dtype, "shape": [1], "names": None}
        self.schema = pa.schema(
            [
                pa.field(
                    key,
                    pa.list_(pa.float32(), 14)
                    if feature["shape"] == [14]
                    else pa.from_numpy_dtype(feature["dtype"]),
                )
                for key, feature in self.features.items()
            ]
        )
        self.stats = {}
        self.episodes, self.total = [], 0
        self.copied_episodes = self.reencoded_episodes = 0
        self.tasks = [task]

    def add_episode(self, source, rows, outcome, copy_sources=None, task=None):
        task = self.task if task is None else task
        if task not in self.tasks:
            self.tasks.append(task)
        task_index = self.tasks.index(task)
        index = len(self.episodes)
        chunk, file = divmod(index, 1000)
        data = self.root / DATA_PATH.format(chunk_index=chunk, file_index=file)
        data.parent.mkdir(parents=True, exist_ok=True)
        stats, n, batch, first_tick, last_tick = {}, 0, [], None, None
        paths = {
            role: self.root
            / VIDEO_PATH.format(
                video_key=f"observation.images.{role}_rgb", chunk_index=chunk, file_index=file
            )
            for role in ROLES
        }
        with ExitStack() as stack:
            parquet = stack.enter_context(pq.ParquetWriter(data, self.schema))
            decoders, encoders, positions, current = {}, {}, {}, {}
            decoder_stack = stack.enter_context(ExitStack())
            previous_source = None
            for _, row in rows:
                row_source = Path(row.get("_segment", source))
                if row_source != previous_source:
                    decoder_stack.close()
                    for role in ROLES:
                        container = decoder_stack.enter_context(
                            av.open(str(row_source / f"{role}.mp4"))
                        )
                        decoders[role] = iter(container.decode(video=0))
                        positions[role] = -1
                    previous_source = row_source
                record = {
                    "observation.state": vector(
                        row["observation_state"]
                        if row["observation_state"] is not None
                        else row["measured_state"]
                    )
                    .astype(np.float32)
                    .tolist(),
                    "action": vector(row["submitted_action"]).astype(np.float32).tolist(),
                    "complementary_info.measured_state": vector(row["measured_state"])
                    .astype(np.float32)
                    .tolist(),
                    "timestamp": n / self.fps,
                    "frame_index": n,
                    "episode_index": index,
                    "index": self.total + n,
                    "task_index": task_index,
                    "complementary_info.is_intervention": bool(row["is_intervention"]),
                    "complementary_info.action_source": {"human": 0, "policy": 1, "hold": 2}[
                        row["source"]
                    ],
                    "complementary_info.event": sum(
                        {
                            "takeover_applied": 1,
                            "human_started": 2,
                            "resume_requested": 4,
                            "policy_started": 8,
                        }[e]
                        for e in row.get("transitions", [])
                    ),
                    "complementary_info.intervention_id": row.get("intervention_id") or 0,
                    "complementary_info.source_tick": row["tick"],
                    "complementary_info.observation_valid": bool(
                        row.get("observation_valid", row["observation_state"] is not None)
                    ),
                    "complementary_info.expert_valid": bool(row.get("expert_valid", False)),
                    "complementary_info.control_time": row["time"],
                    "complementary_info.event_requested_at": row.get("event_requested_at") or -1.0,
                    "complementary_info.event_applied_at": row.get("event_applied_at") or -1.0,
                }
                for key, value in record.items():
                    values = np.asarray(value).reshape(1, -1)
                    stats.setdefault(key, Moments()).add(values)
                    self.stats.setdefault(key, Moments()).add(values)
                for role in ROLES:
                    target = row["video_indices"][role]
                    if target < positions[role]:
                        raise ValueError("video indices moved backwards")
                    while positions[role] < target:
                        try:
                            current[role] = next(decoders[role]).to_ndarray(format="rgb24")
                        except StopIteration as exc:
                            raise ValueError("video shorter than recording indices") from exc
                        positions[role] += 1
                    image = current[role]
                    key = f"observation.images.{role}_rgb"
                    feature = {
                        "dtype": "video",
                        "shape": list(image.shape),
                        "names": ["height", "width", "channels"],
                        "info": {
                            "video.fps": self.fps,
                            "video.codec": "h264",
                            "video.pix_fmt": "yuv420p",
                            "video.is_depth_map": False,
                            "has_audio": False,
                        },
                    }
                    if key in self.features and feature != self.features[key]:
                        raise ValueError("camera shape changed")
                    self.features[key] = feature
                    if copy_sources is None:
                        if role not in encoders:
                            paths[role].parent.mkdir(parents=True, exist_ok=True)
                            output = stack.enter_context(av.open(str(paths[role]), "w"))
                            stream = output.add_stream(
                                "libx264",
                                rate=self.fps,
                                options={"preset": "ultrafast", "crf": "20", "tune": "zerolatency"},
                            )
                            stream.width, stream.height = image.shape[1], image.shape[0]
                            stream.pix_fmt = "yuv420p"
                            stream.codec_context.thread_count = 1
                            encoders[role] = output, stream
                        output, stream = encoders[role]
                        for packet in stream.encode(
                            av.VideoFrame.from_ndarray(image, format="rgb24")
                        ):
                            output.mux(packet)
                    # Exact RGB8 moments via 256-bin histograms, calculated once.
                    # Avoid scanning two large float64 pixel matrices per camera/frame.
                    pixel_count = image.shape[0] * image.shape[1]
                    bins = np.arange(256, dtype=np.float64) / 255
                    counts = np.stack(
                        [np.bincount(image[:, :, c].ravel(), minlength=256) for c in range(3)]
                    )
                    mean = (counts @ bins) / pixel_count
                    var = (counts * (bins[None, :] - mean[:, None]) ** 2).sum(axis=1) / pixel_count
                    low = np.argmax(counts > 0, axis=1) / 255
                    high = (255 - np.argmax(counts[:, ::-1] > 0, axis=1)) / 255
                    stats.setdefault(key, Moments()).merge(pixel_count, mean, var, low, high)
                    self.stats.setdefault(key, Moments()).merge(pixel_count, mean, var, low, high)
                batch.append(record)
                if len(batch) >= 256:
                    parquet.write_table(pa.Table.from_pylist(batch, schema=self.schema))
                    batch.clear()
                first_tick = row["tick"] if first_tick is None else first_tick
                last_tick = row["tick"]
                n += 1
            if batch:
                parquet.write_table(pa.Table.from_pylist(batch, schema=self.schema))
            for output, stream in encoders.values():
                for packet in stream.encode():
                    output.mux(packet)
        if copy_sources is not None:
            self.copied_episodes += 1
            for role in ROLES:
                copied = remux_segments(
                    [p / f"{role}.mp4" for p in copy_sources], paths[role], self.fps
                )
                if copied != n:
                    raise ValueError("copied video frame count does not match samples")
        if copy_sources is None:
            self.reencoded_episodes += 1
        episode = {
            "episode_index": index,
            "tasks": [task],
            "length": n,
            "dataset_from_index": self.total,
            "dataset_to_index": self.total + n,
            "data/chunk_index": chunk,
            "data/file_index": file,
            "meta/episodes/chunk_index": 0,
            "meta/episodes/file_index": 0,
            "source_episode": source.name,
            "source_tick_from": first_tick,
            "source_tick_to": last_tick,
            "outcome": outcome,
        }
        for role in ROLES:
            key = f"videos/observation.images.{role}_rgb"
            episode.update(
                {
                    f"{key}/chunk_index": chunk,
                    f"{key}/file_index": file,
                    f"{key}/from_timestamp": 0.0,
                    f"{key}/to_timestamp": n / self.fps,
                }
            )
        for key, moments in stats.items():
            for stat, value in moments.result(
                image_frames=n if key.startswith("observation.images.") else None
            ).items():
                episode[f"stats/{key}/{stat}"] = value
        self.episodes.append(episode)
        self.total += n

    def finalize(self):
        if not self.episodes:
            return
        meta = self.root / "meta"
        (meta / "episodes/chunk-000").mkdir(parents=True, exist_ok=True)
        pq.write_table(
            pa.Table.from_pylist(self.episodes), meta / "episodes/chunk-000/file-000.parquet"
        )
        tasks = pd.DataFrame(
            {"task_index": list(range(len(self.tasks)))}, index=pd.Index(self.tasks, name="task")
        )
        tasks.to_parquet(meta / "tasks.parquet")
        _json(
            meta / "info.json",
            {
                "codebase_version": "v3.0",
                "robot_type": "yam_bimanual",
                "total_episodes": len(self.episodes),
                "total_frames": self.total,
                "total_tasks": len(self.tasks),
                "chunks_size": 1000,
                "data_files_size_in_mb": 100,
                "video_files_size_in_mb": 200,
                "fps": self.fps,
                "splits": {"train": f"0:{len(self.episodes)}"},
                "data_path": DATA_PATH,
                "video_path": VIDEO_PATH,
                "features": self.features,
            },
        )
        _json(
            meta / "stats.json",
            {
                key: value.result(
                    image_frames=self.total if key.startswith("observation.images.") else None
                )
                for key, value in self.stats.items()
            },
        )


def export_session(source: Path, output: Path, *, expert_only=False, allow_recovered=False):
    source, output = Path(source), Path(output)
    single = (source / "manifest.json").exists()
    if single:
        manifest = json.loads((source / "manifest.json").read_text())
        session = dict(manifest, episodes=[{"path": source.name, "outcome": manifest["outcome"]}])
        source = source.parent
    else:
        session = json.loads((source / "session.json").read_text())
    staging = output.with_name(output.name + ".partial")
    if output.exists() or staging.exists():
        raise FileExistsError("output or partial dataset exists; use a new destination")
    staging.mkdir(parents=True)
    writer, skipped = None, []
    source_files = {}
    for entry in session["episodes"]:
        if entry["outcome"] == "discarded":
            skipped.append({"episode": entry["path"], "reason": "discarded"})
            continue
        episode = source / entry["path"]
        if not episode.resolve().is_relative_to(source.resolve()):
            raise ValueError("episode outside source session")
        manifest = json.loads((episode / "manifest.json").read_text())
        if (
            manifest.get("error")
            or manifest["outcome"] in ("aborted", "discarded", "recording")
            or (manifest["outcome"] == "recovered" and not allow_recovered)
        ):
            skipped.append({"episode": entry["path"], "reason": manifest["outcome"]})
            continue
        for segment in episode_segments(episode):
            for file in segment.iterdir():
                if file.suffix in (".mp4", ".h5", ".jsonl"):
                    source_files[str(file.relative_to(source))] = {
                        "sha256": digest(file),
                        "bytes": file.stat().st_size,
                    }
        fps, task = manifest["fps"], manifest["station"]["task_name"]
        if writer is None:
            writer = DatasetWriter(staging, fps, task)
        elif writer.fps != fps or writer.task != task:
            raise ValueError("mixed frame rates/tasks in one station session")
        # Full, contiguous recordings can preserve the original compressed pixels.
        copy_sources = list(episode_segments(episode))
        previous, counts, copy_ok = None, {}, not expert_only
        first_group = None
        for group, row in _groups(episode):
            segment = row.get("_segment", str(episode))
            count = counts.get(segment, 0)
            if first_group is None:
                first_group = group
            copy_ok &= group == first_group and all(
                row["video_indices"].get(r) == count for r in ROLES
            )
            counts[segment] = count + 1
            previous = row
        if not previous:
            continue
        if expert_only:
            groups = expert_groups(episode)
        else:
            groups = _groups(episode)
        for _, rows in itertools.groupby(groups, key=lambda item: item[0]):
            writer.add_episode(
                episode, rows, manifest["outcome"], copy_sources=copy_sources if copy_ok else None
            )
    if writer:
        writer.finalize()
    report = {
        "schema": "yam_lerobot_export_v2",
        "expert_only": expert_only,
        "source_files": source_files,
        "packet_copy_episodes": 0 if writer is None else writer.copied_episodes,
        "reencoded_episodes": 0 if writer is None else writer.reencoded_episodes,
        "source_session": str(source.resolve()),
        "episodes": 0 if writer is None else len(writer.episodes),
        "frames": 0 if writer is None else writer.total,
        "skipped": skipped,
        "state": "camera-aligned follower feedback; raw feedback is complementary_info.measured_state",
        "action": "absolute command submitted to followers after constraints",
        "action_source": {"0": "human", "1": "policy", "2": "hold"},
        "event_bits": {
            "1": "takeover_applied",
            "2": "human_started",
            "4": "resume_requested",
            "8": "policy_started",
        },
        "timestamps": "nominal fps; original host/device timestamps retained in source HDF5 or legacy JSONL",
        "mock": session.get("mock"),
        "task": session.get("task"),
        "collection_task": session.get("collection_task"),
    }
    _json(staging / "provenance.json", report)
    staging.rename(output)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expert-only", action="store_true", help="仅导出连续有效人工纠正片段")
    parser.add_argument("--allow-recovered", action="store_true", help="明确审核后允许转换恢复数据")
    args = parser.parse_args()
    print(
        json.dumps(
            export_session(
                args.source,
                args.output,
                expert_only=args.expert_only,
                allow_recovered=args.allow_recovered,
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
