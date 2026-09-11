"""Streaming quality checks, preview and explicit multi-task export."""

from __future__ import annotations

import hashlib
import itertools
import json
import time
from collections import Counter
from pathlib import Path

import av
import numpy as np

from ..hil.storage import ROLES, atomic_json, digest, episode_segments, read_rows


def media_files(path):
    path = Path(path).resolve()
    for segment in episode_segments(path):
        for role in ROLES:
            file = (segment / f"{role}.mp4").resolve()
            if not file.is_relative_to(path):
                raise ValueError("媒体文件越出该集目录")
            yield segment, role, file


def check_episode(path, deep=False):
    path = Path(path)
    manifest = json.loads((path / "manifest.json").read_text())
    issues, counts, shapes, content = [], {}, set(), hashlib.sha256()
    for segment, role, file in media_files(path):
        try:
            with av.open(str(file)) as video:
                stream = video.streams.video[0]
                shapes.add((stream.width, stream.height))
                count = (
                    sum(1 for _ in video.decode(video=0))
                    if deep or not stream.frames
                    else stream.frames
                )
                if not count:
                    raise ValueError("无法取得帧数，请运行深度检查")
                counts[(str(segment), role)] = count
            if deep:
                content.update(digest(file).encode())
        except Exception as exc:
            issues.append(f"{segment.name}/{role}: {exc}")
    rows, invalid, bad_vectors, bad_indices, time_errors, gaps = 0, 0, 0, 0, 0, 0
    previous_time, previous_tick = None, None
    for row in read_rows(path):
        rows += 1
        invalid += not row.get("observation_valid", False)
        for key in ("observation_state", "submitted_action"):
            vector = row.get(key)
            if vector is None or np.asarray(vector).shape != (14,) or not np.isfinite(vector).all():
                bad_vectors += 1
        for role in ROLES:
            index = row.get("video_indices", {}).get(role, -1)
            count = counts.get((row.get("_segment", str(path)), role), 0)
            if not isinstance(index, (int, float)) or not 0 <= index < count:
                bad_indices += 1
        stamp, tick = row.get("time"), row.get("tick")
        if (
            stamp is None
            or not np.isfinite(stamp)
            or (previous_time is not None and stamp <= previous_time)
        ):
            time_errors += 1
        if tick is not None and previous_tick is not None and tick != previous_tick + 1:
            gaps += 1
        previous_time, previous_tick = stamp, tick
    if rows != manifest.get("steps"):
        issues.append(f"行数与 manifest 不符：{rows} / {manifest.get('steps')}")
    if not rows:
        issues.append("空集")
    if len(shapes) > 1:
        issues.append("相机分辨率不一致")
    for value, label in (
        (bad_vectors, "状态/动作缺失或非有限值"),
        (bad_indices, "视频索引缺失或越界"),
        (time_errors, "时间戳异常"),
        (gaps, "控制 tick 不连续"),
        (invalid, "观测无效帧"),
    ):
        if value:
            issues.append(f"{label}：{value}")
    if manifest.get("error") or manifest.get("outcome") in (
        "recording",
        "aborted",
        "discarded",
        "recovered",
    ):
        issues.append("来源状态需要人工复核：" + str(manifest.get("outcome")))
    if deep:
        for segment in episode_segments(path):
            for file in sorted(segment.iterdir()):
                if file.suffix in (".h5", ".jsonl"):
                    content.update(digest(file).encode())
    return {
        "ok": not issues,
        "issues": issues,
        "rows": rows,
        "invalid_observations": invalid,
        "bad_indices": bad_indices,
        "bad_vectors": bad_vectors,
        "tick_gaps": gaps,
        "deep": deep,
        "checked_at": time.time(),
        "content_hash": content.hexdigest() if deep else None,
    }


def preview(path, role, frame):
    if role not in ROLES or frame < 0:
        raise ValueError("无效相机或帧号")
    path = Path(path)
    manifest = json.loads((path / "manifest.json").read_text())
    segment_path, local_frame = path, frame
    if manifest.get("schema") == "yam_hil_v2":
        segment_path = None
        for entry, segment in zip(manifest["segments"], episode_segments(path), strict=True):
            if local_frame < entry["steps"]:
                segment_path = segment
                break
            local_frame -= entry["steps"]
        if segment_path is None:
            raise ValueError("帧号超出范围")
    row = next(itertools.islice(read_rows(segment_path), local_frame, local_frame + 1), None)
    if row is not None and segment_path != path:
        row["_segment"] = str(segment_path)
    if row is None:
        raise ValueError("帧号超出范围")
    index = row.get("video_indices", {}).get(role, -1)
    if index < 0:
        raise ValueError("这一帧没有有效图像")
    segment = Path(row.get("_segment", path))
    files = {(str(s), r): p for s, r, p in media_files(path)}
    with av.open(str(files[(str(segment), role)])) as video:
        # Decode at most one raw segment; requests are user-triggered, never 30 Hz.
        image = next(itertools.islice(video.decode(video=0), int(index), int(index) + 1), None)
        if image is None:
            raise ValueError("视频缺帧")
        from io import BytesIO

        result = BytesIO()
        pil = image.to_image()
        pil.thumbnail((960, 540))
        pil.save(result, format="JPEG", quality=80)
        return result.getvalue()


def export_selected(episodes, output, *, expert_only=False, progress=lambda *args: None):
    from ..hil.lerobot_export import DatasetWriter, _groups, expert_groups

    output = Path(output).expanduser().resolve()
    staging = output.with_name(output.name + ".partial")
    if not episodes:
        raise ValueError("请选择需要转换的集")
    manifests, dimensions = [], set()
    for entry in episodes:
        path = Path(entry["path"]).resolve()
        if output.is_relative_to(path) or path.is_relative_to(output):
            raise ValueError("输出目录必须与来源独立")
        manifest = json.loads((path / "manifest.json").read_text())
        if (
            entry["deleted"]
            or manifest.get("error")
            or manifest.get("outcome") in ("recording", "aborted", "discarded", "recovered")
        ):
            raise ValueError(f"{path.name} 尚不可导出，请先复核/恢复来源")
        for _, _, file in media_files(path):
            with av.open(str(file)) as video:
                stream = video.streams.video[0]
                dimensions.add((stream.width, stream.height))
        manifests.append(manifest)
    if len(dimensions) != 1:
        raise ValueError("相机分辨率不同，请分开转换")
    if len({m["fps"] for m in manifests}) != 1:
        raise ValueError("帧率不同，请分开转换；本工具不自动重采样")
    if output.exists() or staging.exists():
        raise ValueError("输出或 .partial 目录已存在，请使用新目录")
    staging.mkdir(parents=True)
    writer = DatasetWriter(staging, manifests[0]["fps"], manifests[0]["station"]["task_name"])
    report = {
        "schema": "yam_curated_export_v1",
        "target": "LeRobot v3.0",
        "expert_only": expert_only,
        "sources": [],
        "started_at": time.time(),
    }
    try:
        for i, (entry, manifest) in enumerate(zip(episodes, manifests, strict=True)):
            path = Path(entry["path"])
            source = {
                "id": entry["id"],
                "path": str(path),
                "label": entry["label"],
                "note": entry["note"],
                "manifest": manifest,
                "files": {},
            }
            for segment in episode_segments(path):
                for file in sorted(segment.iterdir()):
                    if file.suffix in (".h5", ".mp4", ".jsonl"):
                        source["files"][str(file.relative_to(path))] = digest(file)
            counts, first_group, copy_ok = Counter(), None, not expert_only
            for group, row in _groups(path):
                if first_group is None:
                    first_group = group
                segment = row.get("_segment", str(path))
                copy_ok &= group == first_group and all(
                    row["video_indices"].get(r) == counts[segment] for r in ROLES
                )
                counts[segment] += 1
            # Packet copy only when the whole source video matches the selected rows.
            if copy_ok:
                for segment, role, file in media_files(path):
                    with av.open(str(file)) as video:
                        frame_count = video.streams.video[0].frames or sum(
                            1 for _ in video.decode(video=0)
                        )
                        copy_ok &= frame_count == counts[str(segment)]
            rows = expert_groups(path) if expert_only else _groups(path)
            before = len(writer.episodes)
            for _, group_rows in itertools.groupby(rows, key=lambda item: item[0]):
                writer.add_episode(
                    path,
                    group_rows,
                    entry["label"] if entry["label"] != "unreviewed" else manifest["outcome"],
                    copy_sources=list(episode_segments(path)) if copy_ok else None,
                    task=manifest["station"]["task_name"],
                )
            source["output_episodes"] = list(range(before, len(writer.episodes)))
            report["sources"].append(source)
            progress(i + 1, f"{i + 1}/{len(episodes)} 集")
        if not writer.total:
            raise ValueError("所选数据没有可转换帧")
        writer.finalize()
        report.update(
            frames=writer.total,
            episodes=len(writer.episodes),
            tasks=len(writer.tasks),
            packet_copy_episodes=writer.copied_episodes,
            reencoded_episodes=writer.reencoded_episodes,
            finished_at=time.time(),
        )
        atomic_json(staging / "curation.json", report)
        staging.rename(output)
        return report
    except Exception as exc:
        atomic_json(staging / "failure.json", {"error": str(exc), "report": report})
        raise
