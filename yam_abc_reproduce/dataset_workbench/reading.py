"""Bounded random-access previews and LeRobot columnar checks."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from io import BytesIO
from pathlib import Path

import av
import h5py
import numpy as np
import pyarrow.parquet as pq

from ..hil.storage import ROLES, episode_segments, read_rows
from .formats import cameras, contained


class PreviewCache:
    def __init__(self, max_bytes=32 * 1024 * 1024):
        self.max_bytes, self.size = max_bytes, 0
        self.items = OrderedDict()
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(2)

    def get(self, key, producer):
        with self.lock:
            if key in self.items:
                self.items.move_to_end(key)
                return self.items[key]
        if not self.slots.acquire(timeout=5):
            raise ValueError("预览繁忙，请稍后重试")
        try:
            value = producer()
        finally:
            self.slots.release()
        with self.lock:
            previous = self.items.pop(key, b"")
            self.size -= len(previous)
            self.items[key] = value
            self.size += len(value)
            while self.size > self.max_bytes and self.items:
                _, old = self.items.popitem(last=False)
                self.size -= len(old)
        return value


CACHE = PreviewCache()


def jpeg_at(file, seconds, fps):
    with av.open(str(file)) as video:
        stream = video.streams.video[0]
        stream.thread_count = 1
        start = float((stream.start_time or 0) * stream.time_base)
        target = start + seconds
        video.seek(int(target / stream.time_base), stream=stream, backward=True, any_frame=False)
        for frame in video.decode(stream):
            if frame.time is not None and float(frame.time) + 0.5 / fps >= target:
                # Do not return a later unrelated frame across a timestamp gap.
                if abs(float(frame.time) - target) > 1.1 / fps:
                    raise ValueError("目标时间附近缺少视频帧")
                image = frame.to_image()
                image.thumbnail((960, 540))
                buffer = BytesIO()
                image.save(buffer, format="JPEG", quality=80)
                return buffer.getvalue()
        raise ValueError("视频缺帧")


def preview_entry(entry, camera, frame):
    m, root = entry["metadata"], Path(entry["path"])
    if not 0 <= frame < int(m["steps"]):
        raise ValueError("帧号超出范围")
    if m.get("_format") == "lerobot_v3":
        media = m["_media"].get(camera)
        if media is None:
            raise ValueError("相机不存在")
        file = contained(root, media["path"])
        seconds = media["from"] + frame / m["fps"]
    else:
        if camera not in ROLES:
            raise ValueError("相机不存在")
        segment, local = root, frame
        if m.get("schema") == "yam_hil_v2":
            segment = None
            for item, path in zip(m["segments"], episode_segments(root), strict=True):
                if local < item["steps"]:
                    segment = path
                    break
                local -= item["steps"]
            if segment is None:
                raise ValueError("帧号超出分段索引")
        if (segment / "samples.h5").exists():
            file_h5 = contained(root, (segment / "samples.h5").relative_to(root))
            with h5py.File(file_h5, "r", libver="latest", swmr=True) as h5:
                if local >= int(h5["committed_rows"][()]):
                    raise ValueError("帧尚未提交")
                index = int(h5["video_indices"][local, ROLES.index(camera)])
        else:
            import itertools

            row = next(itertools.islice(read_rows(root), frame, frame + 1), None)
            index = -1 if row is None else row.get("video_indices", {}).get(camera, -1)
        if index < 0:
            raise ValueError("这一帧没有有效图像")
        file = contained(root, (segment / f"{camera}.mp4").relative_to(root))
        seconds = index / m["fps"]
    stat = file.stat()
    key = (str(file), stat.st_size, stat.st_mtime_ns, seconds, m["fps"])
    return CACHE.get(key, lambda: jpeg_at(file, seconds, m["fps"]))


def check_lerobot(entry, deep=False):
    m, root = entry["metadata"], Path(entry["path"])
    issues, rows, invalid = [], 0, 0
    table = pq.ParquetFile(contained(root, m["_data_file"]))
    keys = ["episode_index", "frame_index", "timestamp", "observation.state", "action"]
    available = table.schema_arrow.names
    for key in keys:
        if key not in available:
            issues.append("缺少列：" + key)
    if issues:
        return dict(ok=False, issues=issues, rows=0, deep=deep, checked_at=time.time())
    target = int(m["_episode_key"])
    previous = None
    for group in range(table.num_row_groups):
        # Row group pruning avoids reading unrelated episodes in shared shards.
        idx = table.schema.names.index("episode_index")
        stats = table.metadata.row_group(group).column(idx).statistics
        if stats and stats.has_min_max and (target < stats.min or target > stats.max):
            continue
        for batch in table.iter_batches(batch_size=1024, row_groups=[group], columns=keys):
            values = batch.to_pydict()
            for i, ep in enumerate(values["episode_index"]):
                if ep != target:
                    continue
                if values["frame_index"][i] != rows:
                    invalid += 1
                stamp = values["timestamp"][i]
                if (
                    stamp is None
                    or not np.isfinite(stamp)
                    or (previous is not None and stamp <= previous)
                ):
                    invalid += 1
                previous = stamp
                for key in ("observation.state", "action"):
                    value = values[key][i]
                    shape = m["_features"][key].get("shape")
                    if (
                        value is None
                        or (shape and list(np.asarray(value).shape) != shape)
                        or not np.isfinite(value).all()
                    ):
                        invalid += 1
                rows += 1
    if rows != m["steps"]:
        issues.append(f"行数不一致：{rows}/{m['steps']}")
    if invalid:
        issues.append(f"帧号、时间或数值异常：{invalid}")
    for camera in cameras(m):
        media = m["_media"][camera]
        file = contained(root, media["path"])
        try:
            with av.open(str(file)) as video:
                stream = video.streams.video[0]
                stream.thread_count = 1
                if media["from"] < 0 or media["to"] <= media["from"]:
                    raise ValueError("无效的时间范围")
                if deep:
                    start = float((stream.start_time or 0) * stream.time_base)
                    video.seek(
                        int((start + media["from"]) / stream.time_base),
                        stream=stream,
                        backward=True,
                    )
                    count = 0
                    for frame in video.decode(stream):
                        stamp = float(frame.time) - start
                        if stamp >= media["to"] - 0.5 / m["fps"]:
                            break
                        if stamp >= media["from"] - 0.5 / m["fps"]:
                            count += 1
                    if count != rows:
                        raise ValueError(f"视频范围帧数不符：{count}/{rows}")
                elif (
                    stream.duration
                    and float(stream.duration * stream.time_base) + 1 / m["fps"] < media["to"]
                ):
                    raise ValueError("视频短于episode时间范围")
        except Exception as exc:
            issues.append(f"{camera}: {exc}")
    return dict(ok=not issues, issues=issues, rows=rows, deep=deep, checked_at=time.time())
