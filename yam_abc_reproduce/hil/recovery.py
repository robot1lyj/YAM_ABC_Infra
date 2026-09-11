"""Offline recovery into a NEW episode; never edits source recordings."""

import argparse
import json
import shutil
from pathlib import Path

import av

from .storage import ROLES, Samples, atomic_json, h5_rows


def video_count(path):
    count = 0
    try:
        with av.open(str(path)) as container:
            for _ in container.decode(video=0):
                count += 1
    except (av.FFmpegError, OSError):
        pass
    return count


def prefix_video(source, destination, count, fps):
    with av.open(str(source)) as inp, av.open(str(destination), "w") as out:
        stream = None
        written = 0
        for frame in inp.decode(video=0):
            if written >= count:
                break
            if stream is None:
                stream = out.add_stream(
                    "libx264",
                    rate=int(fps),
                    options={"preset": "ultrafast", "crf": "20", "tune": "zerolatency"},
                )
                stream.width, stream.height = frame.width, frame.height
                stream.pix_fmt, stream.thread_count = "yuv420p", 1
            frame.pts = None
            for packet in stream.encode(frame):
                out.mux(packet)
            written += 1
        if written != count:
            raise ValueError("video truncated during recovery")
        if stream:
            for packet in stream.encode():
                out.mux(packet)


def recover(source, output):
    source, output = Path(source), Path(output)
    if output.resolve().is_relative_to(source.resolve()):
        raise ValueError("recovery output must be outside source episode")
    manifest = json.loads((source / "manifest.json").read_text())
    if manifest["schema"] != "yam_hil_v2":
        raise ValueError("recovery currently requires HDF5 v2 recordings")
    output.mkdir(parents=True, exist_ok=False)
    entries, errors, total = [], [], 0
    for segment in sorted(source.glob("segment_*")):
        try:
            counts = {r: video_count(segment / f"{r}.mp4") for r in ROLES}
            valid = 0
            leading = 0
            for row in h5_rows(segment / "samples.h5"):
                if not row["video_indices"] and valid == 0:
                    leading += 1
                    continue
                if not all(
                    row["video_indices"].get(r) == valid and valid < counts[r] for r in ROLES
                ):
                    break
                valid += 1
            if not valid:
                raise ValueError("no common recoverable RGB/sample prefix")
            dst = output / segment.name
            dst.mkdir()
            samples = Samples(dst / "samples.h5")
            try:
                for index, row in enumerate(h5_rows(segment / "samples.h5")):
                    if index < leading:
                        continue
                    if index >= valid + leading:
                        break
                    samples.append(row)
            finally:
                samples.close()
            for role in ROLES:
                if counts[role] == valid and (segment / "segment.json").exists():
                    shutil.copy2(segment / f"{role}.mp4", dst / f"{role}.mp4")
                else:
                    prefix_video(
                        segment / f"{role}.mp4", dst / f"{role}.mp4", valid, manifest["fps"]
                    )
            entry = {
                "path": dst.name,
                "steps": valid,
                "start_frame": total,
                "video_frames": {r: valid for r in ROLES},
                "state": "recovered",
            }
            atomic_json(dst / "segment.json", entry)
            entries.append(entry)
            total += valid
        except Exception as exc:
            errors.append({"segment": segment.name, "error": str(exc)})
    manifest.update(
        segments=entries,
        steps=total,
        outcome="recovered",
        error=None,
        recovery={"source": str(source.resolve()), "errors": errors, "requires_review": True},
    )
    atomic_json(output / "manifest.json", manifest)
    return {"frames": total, "segments": len(entries), "errors": errors, "requires_review": True}


def main():
    parser = argparse.ArgumentParser(description="恢复异常 HDF5 采集集到新目录，保留原始文件")
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(recover(args.source, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
