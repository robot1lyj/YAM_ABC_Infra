"""Offline recovery into a NEW episode; never edits source recordings."""

import argparse
import heapq
import json
import pickle
from contextlib import ExitStack
from pathlib import Path

import av
import numpy as np

from ..storage_health import require_recording_storage
from .storage import ROLES, SCALARS, VECTORS, SegmentWriter, h5_rows, json_value


class _SpoolUnpickler(pickle.Unpickler):
    """Read the recorder's arrays and primitive containers, never arbitrary code."""

    def find_class(self, module, name):
        if (module, name) in {
            ("numpy", "ndarray"), ("numpy", "dtype"),
            ("numpy.core.multiarray", "_reconstruct"),
            ("numpy._core.multiarray", "_reconstruct"),
            ("numpy.core.multiarray", "scalar"),
            ("numpy._core.multiarray", "scalar"),
            ("numpy.core.numeric", "_frombuffer"),
            ("numpy._core.numeric", "_frombuffer"),
        }:
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"unsupported spool object: {module}.{name}")


def _frame_key(row):
    """Runtime tick and host clock survive HDF5 encoding and wait filtering."""
    tick, stamp = row.get("tick"), row.get("time")
    if (not isinstance(tick, (int, np.integer)) or isinstance(tick, (bool, np.bool_))
            or tick < 0 or not isinstance(stamp, (int, float, np.number))
            or not np.isfinite(stamp)):
        raise ValueError("frame lacks a valid original tick/time identity")
    return int(tick), float(stamp)


def _segment_frames(source, errors):
    previous = None
    for segment in sorted(source.glob("segment_*")):
        try:
            with ExitStack() as stack:
                videos = {
                    role: iter(stack.enter_context(av.open(str(segment / f"{role}.mp4"))).decode(video=0))
                    for role in ROLES
                }
                valid = 0
                for row in h5_rows(segment / "samples.h5"):
                    if not row["video_indices"] and valid == 0:
                        continue
                    if not all(row["video_indices"].get(role) == valid for role in ROLES):
                        raise ValueError("RGB/sample indices stop matching the common prefix")
                    try:
                        images = {role: next(videos[role]).to_ndarray(format="rgb24") for role in ROLES}
                    except StopIteration:
                        raise ValueError("video truncated before the committed sample range ends") from None
                    key = _frame_key(row)
                    if previous is not None and key <= previous:
                        raise ValueError("segment frame identities are not strictly increasing")
                    previous = key
                    valid += 1
                    yield key, row, images, "segment", segment.name
                if not valid:
                    raise ValueError("no common recoverable RGB/sample prefix")
        except Exception as exc:
            errors.append({"segment": segment.name, "error": str(exc)})


def _spool_frames(source, errors, stats):
    previous = None
    for path in sorted((source / ".recording-spool").glob("*")):
        stats["files"] += 1
        try:
            if path.suffix != ".pkl" or not path.stem.isdigit() or not path.is_file():
                raise ValueError("incomplete or unrecognized recording buffer file")
            with path.open("rb") as stream:
                row, images = _SpoolUnpickler(stream).load()
                if stream.read(1):
                    raise ValueError("unexpected trailing bytes in recording buffer")
            if not isinstance(row, dict) or not isinstance(images, dict):
                raise ValueError("expected a recording row and camera images")
            key = _frame_key(row)
            if previous is not None and key < previous:
                raise ValueError("buffer frame identities are out of order")
            if set(images) != set(ROLES):
                raise ValueError("buffer frame is missing the complete three-camera RGB set")
            for image in images.values():
                if (not isinstance(image, np.ndarray) or image.dtype != np.uint8
                        or image.ndim != 3 or image.shape[2] != 3 or min(image.shape[:2]) < 1):
                    raise ValueError("expected named RGB8 camera frames")
            for name in VECTORS:
                value = row.get(name)
                if value is not None:
                    vector = np.asarray(value, dtype=np.float64)
                    if vector.shape != (14,) or not np.isfinite(vector).all():
                        raise ValueError(f"buffer {name} must have 14 finite values")
            for name, dtype in SCALARS.items():
                value = row.get(name)
                if value is None:
                    continue
                if dtype == "?":
                    valid = isinstance(value, (bool, np.bool_))
                elif dtype == "i8":
                    valid = (isinstance(value, (int, np.integer))
                             and not isinstance(value, (bool, np.bool_))
                             and np.iinfo(np.int64).min <= value <= np.iinfo(np.int64).max)
                else:
                    valid = isinstance(value, (int, float, np.number)) and np.isfinite(value)
                if not valid:
                    raise ValueError(f"invalid buffer scalar {name}")
            # Reject malformed metadata before it contaminates a pending HDF5 batch.
            json.dumps(row, default=json_value, allow_nan=False)
            previous = key
            yield key, row, images, "spool", path.name
        except Exception as exc:
            stats["unrecoverable_files"] += 1
            errors.append({"spool": path.name, "error": f"{type(exc).__name__}: {exc}"})


def recover(source, output):
    source, output = Path(source), Path(output)
    if output.resolve().is_relative_to(source.resolve()):
        raise ValueError("recovery output must be outside source episode")
    manifest_path = source / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("recovery requires the source manifest for fps and recording identity; buffer files alone are insufficient")
    manifest = json.loads(manifest_path.read_text())
    if manifest["schema"] != "yam_hil_v2":
        raise ValueError("recovery currently requires HDF5 v2 recordings")
    require_recording_storage(output)
    output.mkdir(parents=True, exist_ok=False)
    errors = []
    stats = dict(files=0, recovered_frames=0, duplicate_frames=0, unrecoverable_files=0)
    recovery = dict(source=str(source.resolve()), source_episode_id=manifest.get("episode_id"),
                    errors=errors, spool=stats, requires_review=True)
    writer = SegmentWriter(output, manifest["fps"], dict(manifest, recovery=recovery),
                           video_backend="libx264")
    previous = None
    previous_shapes = None
    # Both sources are ordered by original control time. On an exact overlap,
    # heapq.merge prefers the segment, which is already readable from disk.
    frames = heapq.merge(_segment_frames(source, errors), _spool_frames(source, errors, stats),
                        key=lambda item: item[0])
    try:
        for key, row, images, kind, _path in frames:
            if key == previous:
                if kind == "spool":
                    stats["duplicate_frames"] += 1
                continue
            shapes = {role: image.shape for role, image in images.items()}
            if previous_shapes is not None and shapes != previous_shapes:
                writer.finish_segment()
            writer.append(row, images)
            previous, previous_shapes = key, shapes
            if kind == "spool":
                stats["recovered_frames"] += 1
    except Exception as exc:
        writer.error = f"recovery output failed: {type(exc).__name__}: {exc}"
        raise
    finally:
        frames.close()
        writer.close("aborted" if writer.error else "recovered")
    return {"frames": writer.written, "segments": len(writer.segments), "errors": errors,
            "spool": stats, "requires_review": True}


def main():
    parser = argparse.ArgumentParser(description="恢复异常 HDF5 采集集及录制缓冲到新目录，保留原始文件")
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(recover(args.source, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
