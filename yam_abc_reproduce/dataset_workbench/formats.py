"""Metadata-first format adapters. No torch or robot dependencies."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pyarrow.parquet as pq

RAW_FORMATS = {"yam_hil_v1": "原始 · JSONL＋MP4", "yam_hil_v2": "原始 · HDF5＋MP4"}


def contained(root, relative):
    root = Path(root).resolve()
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("数据路径必须是来源内的相对路径")
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("数据路径越出来源目录")
    return path


def fingerprint(paths):
    h = hashlib.sha256()
    for path in paths:
        st = path.stat()
        h.update(f"{path.name}:{st.st_size}:{st.st_mtime_ns}".encode())
    return h.hexdigest()


def raw_metadata(path):
    file = path / "manifest.json"
    m = json.loads(file.read_text())
    if m.get("schema") not in RAW_FORMATS:
        raise ValueError("不支持的 YAM 格式")
    if m.get("outcome") == "recording":
        raise ValueError("录制尚未结束")
    for segment in m.get("segments", []):
        contained(path, segment["path"])
    task = m.get("collection_task") or {}
    m["_format"] = m["schema"]
    m["_title"] = task.get("name") or m.get("station", {}).get("task_name", "未命名任务")
    m["_task"] = m.get("station", {}).get("task_name", "")
    m["_episode_key"] = ""
    sources = [file]
    segments = m.get("segments", []) if m["schema"] == "yam_hil_v2" else [{"path": "."}]
    for segment in segments:
        folder = contained(path, segment["path"])
        for name in ("samples.h5", "steps.jsonl", "top.mp4", "left.mp4", "right.mp4"):
            source = folder / name
            if source.exists():
                sources.append(source)
    return m, fingerprint(sources)


def lerobot_metadata(root):
    """Read metadata in bounded batches; frame tables/video are untouched."""
    info_path = root / "meta/info.json"
    info = json.loads(info_path.read_text())
    if info.get("codebase_version") != "v3.0":
        raise ValueError("当前支持 LeRobot v3.0，其他版本需先升级")
    fps = float(info.get("fps", 0))
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError("LeRobot fps必须为正有限值")
    videos = {k: v for k, v in info.get("features", {}).items() if v.get("dtype") == "video"}
    if not videos:
        raise ValueError("当前 LeRobot 浏览适配支持 MP4 video 特征，不支持嵌入式图片")
    files = sorted((root / "meta/episodes").glob("chunk-*/*.parquet"))
    if not files:
        raise ValueError("缺少 LeRobot episode 元数据")
    for file in files:
        token = fingerprint([info_path, file])
        parquet = pq.ParquetFile(file)
        for batch in parquet.iter_batches(batch_size=256):
            for ep in batch.to_pylist():
                index, length = int(ep["episode_index"]), int(ep["length"])
                if index < 0 or length <= 0:
                    raise ValueError("无效的 episode_index 或 length")
                task = "; ".join(str(t) for t in ep.get("tasks", []))
                # Statistics can be large; keep only location/identity fields here.
                locator = {k: v for k, v in ep.items() if not k.startswith("stats/")}
                data_file = info["data_path"].format(
                    chunk_index=ep["data/chunk_index"], file_index=ep["data/file_index"]
                )
                contained(root, data_file)
                media = {}
                for key in videos:
                    prefix = f"videos/{key}"
                    path = info["video_path"].format(
                        video_key=key,
                        chunk_index=ep[prefix + "/chunk_index"],
                        file_index=ep[prefix + "/file_index"],
                    )
                    contained(root, path)
                    media[key] = {
                        "path": path,
                        "from": float(ep[prefix + "/from_timestamp"]),
                        "to": float(ep[prefix + "/to_timestamp"]),
                    }
                    start, end = media[key]["from"], media[key]["to"]
                    if (
                        not math.isfinite(start)
                        or not math.isfinite(end)
                        or start < 0
                        or end <= start
                    ):
                        raise ValueError("LeRobot视频范围无效")
                yield (
                    dict(
                        _format="lerobot_v3",
                        _title=task or f"Episode {index}",
                        _task=task,
                        _episode_key=str(index),
                        _locator=locator,
                        _media=media,
                        _data_file=data_file,
                        _features=info["features"],
                        fps=info["fps"],
                        steps=length,
                        schema="lerobot_v3",
                        outcome=ep.get("outcome", "unknown"),
                        station={"task_name": task},
                    ),
                    token,
                )


def cameras(metadata):
    if metadata.get("_format") == "lerobot_v3":
        keys = list(metadata["_media"])

        def sort_key(key):
            for i, role in enumerate(("top", "left", "right")):
                if key == f"observation.images.{role}_rgb":
                    return i
            return 3

        return sorted(keys, key=sort_key)
    return ["top", "left", "right"]
