"""Versioned, bounded HDF5 samples and independently committed video segments."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

import h5py
import numpy as np

ROLES = ("top", "left", "right")
VECTORS = (
    "measured_state",
    "observation_state",
    "leader_state",
    "submitted_action",
    "selected_action",
    "policy_action",
    "human_action",
)
CAMERA_FIELDS = ("host_received_at", "device_timestamp_ms", "sequence", "device_frame_number")
SCALARS = {
    "tick": "i8",
    "time": "f8",
    "epoch": "i8",
    "intervention_id": "i8",
    "event_requested_at": "f8",
    "event_applied_at": "f8",
    "is_intervention": "?",
    "expert_valid": "?",
    "observation_valid": "?",
}


def json_value(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(type(value).__name__)


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w") as stream:
        json.dump(value, stream, default=json_value, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


class Samples:
    """Single writer, fixed schema, small batches. No image payload in HDF5."""

    def __init__(self, path, batch_size=15):
        self.file = h5py.File(path, "w", libver="latest")
        self.file.attrs["schema"] = "yam_samples_v2"
        self.batch_size, self.pending = batch_size, []
        for key in (*VECTORS, *SCALARS):
            shape = (14,) if key in VECTORS else ()
            self.file.create_dataset(
                key,
                shape=(0, *shape),
                maxshape=(None, *shape),
                chunks=(batch_size, *shape),
                dtype="f8" if shape else SCALARS[key],
            )
            self.file.create_dataset(
                key + "__valid", shape=(0,), maxshape=(None,), chunks=(batch_size,), dtype="?"
            )
        self.file.create_dataset(
            "video_indices", shape=(0, 3), maxshape=(None, 3), chunks=(batch_size, 3), dtype="i8"
        )
        for key in CAMERA_FIELDS:
            self.file.create_dataset(
                "camera_" + key,
                shape=(0, 3),
                maxshape=(None, 3),
                chunks=(batch_size, 3),
                dtype="f8",
            )
            self.file.create_dataset(
                "camera_" + key + "__valid",
                shape=(0, 3),
                maxshape=(None, 3),
                chunks=(batch_size, 3),
                dtype="?",
            )
        self.file.create_dataset(
            "details", shape=(0,), maxshape=(None,), chunks=(batch_size,), dtype=h5py.string_dtype()
        )
        self.file.create_dataset("committed_rows", data=np.int64(0))
        self.file.flush()
        self.file.swmr_mode = True

    def append(self, row):
        self.pending.append(row)
        if len(self.pending) >= self.batch_size:
            self.flush()

    def flush(self):
        if not self.pending:
            return
        rows, self.pending = self.pending, []
        start = int(self.file["committed_rows"][()])
        end = start + len(rows)
        for key in (*VECTORS, *SCALARS):
            ds = self.file[key]
            values = [r.get(key) for r in rows]
            ds.resize(end, axis=0)
            ds[start:end] = [
                v if v is not None else np.zeros(ds.shape[1:], dtype=ds.dtype) for v in values
            ]
            valid = self.file[key + "__valid"]
            valid.resize(end, axis=0)
            valid[start:end] = [v is not None for v in values]
        for key, values in (
            (
                "video_indices",
                [[r.get("video_indices", {}).get(role, -1) for role in ROLES] for r in rows],
            ),
            (
                "details",
                [
                    json.dumps(
                        {
                            k: v
                            for k, v in r.items()
                            if k not in (*VECTORS, *SCALARS, "video_indices")
                        },
                        default=json_value,
                        allow_nan=False,
                    )
                    for r in rows
                ],
            ),
        ):
            ds = self.file[key]
            ds.resize(end, axis=0)
            ds[start:end] = values
        for key in CAMERA_FIELDS:
            values = [
                [(r.get("sync") or {}).get("cameras", {}).get(role, {}).get(key) for role in ROLES]
                for r in rows
            ]
            for suffix, data in (
                ("", [[0 if v is None else v for v in item] for item in values]),
                ("__valid", [[v is not None for v in item] for item in values]),
            ):
                ds = self.file["camera_" + key + suffix]
                ds.resize(end, axis=0)
                ds[start:end] = data
        self.file.flush()
        self.file["committed_rows"][()] = end
        self.file.flush()

    def close(self):
        try:
            self.flush()
        finally:
            self.file.close()


def h5_rows(path):
    with h5py.File(path, "r", libver="latest", swmr=True) as f:
        size = int(f["committed_rows"][()])
        if any(len(ds) < size for key, ds in f.items() if key != "committed_rows"):
            raise ValueError("HDF5 committed range exceeds datasets")
        for start in range(0, size, 256):
            block = {
                key: ds[start : min(size, start + 256)]
                for key, ds in f.items()
                if key != "committed_rows"
            }
            for i in range(len(block["details"])):
                row = json.loads(block["details"][i])
                for key in (*VECTORS, *SCALARS):
                    value = block[key][i]
                    row[key] = (
                        (value.tolist() if isinstance(value, np.ndarray) else value.item())
                        if block[key + "__valid"][i]
                        else None
                    )
                row["video_indices"] = {
                    r: int(v)
                    for r, v in zip(ROLES, block["video_indices"][i], strict=True)
                    if v >= 0
                }
                yield row


def episode_segments(path):
    path = Path(path)
    manifest = json.loads((path / "manifest.json").read_text())
    if manifest["schema"] == "yam_hil_v1":
        yield path
    elif manifest["schema"] == "yam_hil_v2":
        for entry in manifest["segments"]:
            segment = path / entry["path"]
            if not segment.resolve().is_relative_to(path.resolve()):
                raise ValueError("segment outside episode")
            yield segment
    else:
        raise ValueError("unsupported recording schema")


def read_rows(path):
    """Compatibility reader: episode, segment, or legacy JSONL path."""
    path = Path(path)
    if path.is_file():
        with path.open() as stream:
            for line in stream:
                yield json.loads(line)
    elif (path / "samples.h5").exists():
        yield from h5_rows(path / "samples.h5")
    else:
        for segment in episode_segments(path):
            source = segment if (segment / "samples.h5").exists() else segment / "steps.jsonl"
            for row in read_rows(source):
                yield dict(row, _segment=str(segment))


class SegmentWriter:
    def __init__(
        self,
        path,
        fps,
        metadata,
        segment_seconds=60,
        min_free_bytes=512 * 1024**2,
        video_backend=None,
    ):
        if (
            not np.isfinite(fps)
            or fps <= 0
            or not np.isfinite(segment_seconds)
            or segment_seconds <= 0
        ):
            raise ValueError("positive finite fps/segment duration required")
        self.path, self.fps, self.metadata = Path(path), fps, metadata
        self.limit = max(1, int(fps * segment_seconds))
        self.min_free_bytes = min_free_bytes
        self.segments, self.videos, self.counts = [], {}, {}
        self.video_backend = video_backend
        if video_backend is not None:
            self.metadata["video_encoder"] = video_backend
        self.samples = None
        self.written = self.local = 0
        self.error = None
        self.episode_id = uuid.uuid4().hex
        # Camera streams are independent. One worker per stream avoids serial software
        # encoding and prevents pipe writes from serializing the RKMPP subprocesses.
        self.encoder_pool = ThreadPoolExecutor(max_workers=len(ROLES), thread_name_prefix="video")
        self.checkpoint("recording")

    def checkpoint(self, outcome):
        atomic_json(
            self.path / "manifest.json",
            dict(
                self.metadata,
                schema="yam_hil_v2",
                episode_id=self.episode_id,
                fps=self.fps,
                steps=self.written,
                segments=self.segments,
                outcome=outcome,
                episode_success=outcome if outcome in ("success", "failure") else None,
                error=self.error,
                clock="RK host monotonic; camera arrival alignment",
                action_semantics="absolute submitted follower target after constraints",
            ),
        )

    def append(self, row, images):
        if self.samples is None:
            if shutil.disk_usage(self.path).free < self.min_free_bytes:
                raise OSError("insufficient free disk space for recording")
            self.current = self.path / f"segment_{len(self.segments):06d}"
            self.current.mkdir()
            self.samples = Samples(self.current / "samples.h5")
            self.local, self.videos, self.counts = 0, {}, {}
        if self.local % int(max(1, self.fps)) == 0:
            if shutil.disk_usage(self.path).free < self.min_free_bytes:
                raise OSError("recording stopped: low disk space")
        if images and self.video_backend is None:
            from .video import select_backend

            self.video_backend = select_backend(next(iter(images.values())), int(self.fps))
            self.metadata["video_encoder"] = self.video_backend
            self.checkpoint("recording")
        indices = {}
        for role, im in images.items():
            if role not in ROLES or im.dtype != np.uint8 or im.ndim != 3 or im.shape[2] != 3:
                raise ValueError("expected named RGB8 camera frames")
            if role not in self.videos:
                from .video import open_video

                self.videos[role] = open_video(
                    self.video_backend,
                    self.current / f"{role}.mp4",
                    im.shape[1],
                    im.shape[0],
                    int(self.fps),
                )
                self.counts[role] = 0
            video = self.videos[role]
            if im.shape != (video.height, video.width, 3):
                raise ValueError("camera resolution changed during recording")
            indices[role] = self.counts[role]

        futures = []
        for role, im in images.items():
            futures.append(self.encoder_pool.submit(self.videos[role].append, im))
        wait(futures)
        for future in futures:
            future.result()
        for role in images:
            self.counts[role] += 1
        self.samples.append(dict(row, video_indices=indices))
        self.local += 1
        self.written += 1
        if self.local >= self.limit:
            self.finish_segment()

    def finish_segment(self):
        if self.samples is None:
            return
        samples, self.samples = self.samples, None
        close_error = None
        try:
            samples.close()
        finally:
            for video in self.videos.values():
                try:
                    video.close()
                except Exception as exc:  # noqa: BLE001
                    close_error = close_error or exc
            self.videos = {}
        if close_error:
            raise close_error
        files = {}
        for path in self.current.iterdir():
            if path.suffix in (".h5", ".mp4"):
                with path.open("rb") as f:
                    os.fsync(f.fileno())
                files[path.name] = {"bytes": path.stat().st_size}
        # Hashing large video files is deferred to offline validation.
        entry = {
            "path": self.current.name,
            "steps": self.local,
            "start_frame": self.written - self.local,
            "video_frames": self.counts,
            "files": files,
            "state": "committed",
        }
        atomic_json(self.current / "segment.json", entry)
        self.segments.append(entry)
        self.checkpoint("recording")

    def close(self, outcome, metadata=None):
        try:
            if metadata:
                self.metadata.update(metadata)
            self.finish_segment()
            self.checkpoint(outcome)
        finally:
            self.encoder_pool.shutdown(wait=True)
