"""Offline synthetic catalog scaling and optional one-hour tiny-video seek benchmark."""

import argparse
import json
import resource
import shutil
import statistics
import time
from pathlib import Path

from yam_abc_reproduce.dataset_workbench.catalog import Catalog


def measure(fn, repeats=20):
    times = []
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        times.append((time.perf_counter() - start) * 1000)
    return dict(
        median_ms=statistics.median(times), p95_ms=sorted(times)[int(0.95 * (len(times) - 1))]
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="新的临时测试目录，不使用生产数据")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--long-preview", action="store_true")
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=False)
    catalog = Catalog(args.root / "catalog")
    result = {
        "scope": "synthetic local metadata; not 100 GB real capture throughput",
        "catalog": {},
    }
    for total, previous in [(10000, 0), (100000, 10000)]:
        start = time.perf_counter()
        with catalog.db() as db:
            for i in range(previous, total):
                catalog.upsert(
                    db,
                    args.root / f"sources/{i}",
                    dict(
                        schema="yam_hil_v2",
                        steps=108000,
                        fps=30,
                        station={"task_name": f"sort lego {i % 10}"},
                    ),
                    str(i),
                    identity=f"{i:012d}",
                    label="failure" if i % 10 == 0 else "unreviewed",
                )
        result["catalog"][str(total)] = {
            "insert_seconds": time.perf_counter() - start,
            "first_page": measure(catalog.listing),
            "deep_cursor": measure(lambda: catalog.listing(cursor=f"{total - 101:012d}")),
            "failure_filter": measure(lambda: catalog.listing(view="failure")),
            "search": measure(lambda: catalog.listing(query="lego 9")),
            "first_page_json_bytes": len(json.dumps(catalog.listing()).encode()),
        }
    if args.long_preview:
        import av
        import h5py
        import numpy as np

        from yam_abc_reproduce.dataset_workbench.reading import CACHE, preview_entry

        root = args.root / "hour_raw"
        segment = root / "segment_000000"
        segment.mkdir(parents=True)
        n = 108000
        with h5py.File(segment / "samples.h5", "w") as h5:
            h5.create_dataset("committed_rows", data=n)
            h5.create_dataset(
                "video_indices", data=np.tile(np.arange(n)[:, None], (1, 3)), chunks=(256, 3)
            )
        with av.open(str(segment / "top.mp4"), "w") as video:
            stream = video.add_stream("libx264", rate=30)
            stream.width = stream.height = 32
            stream.pix_fmt = "yuv420p"
            stream.thread_count = 1
            stream.options = {"preset": "ultrafast", "tune": "zerolatency", "crf": "20"}
            stream.codec_context.gop_size = 30
            for i in range(n):
                frame = av.VideoFrame.from_ndarray(
                    np.full((32, 32, 3), i % 200, dtype=np.uint8), format="rgb24"
                )
                for packet in stream.encode(frame):
                    video.mux(packet)
            for packet in stream.encode():
                video.mux(packet)
        for role in ("left", "right"):
            shutil.copyfile(segment / "top.mp4", segment / f"{role}.mp4")
        manifest = dict(
            schema="yam_hil_v2",
            outcome="success",
            fps=30,
            steps=n,
            station={"task_name": "synthetic seek"},
            segments=[dict(path="segment_000000", steps=n)],
        )
        (root / "manifest.json").write_text(json.dumps(manifest))
        catalog.scan(root)
        entry = catalog.get(catalog.listing(query="synthetic seek")["episodes"][0]["id"])

        def cold():
            CACHE.items.clear()
            CACHE.size = 0
            for role in ("top", "left", "right"):
                preview_entry(entry, role, n - 1)

        result["one_hour_preview"] = {
            "frames": n,
            "resolution": [32, 32],
            "cold_app_cache_three_cameras": measure(cold, 5),
            "warm_cache_one_camera": measure(lambda: preview_entry(entry, "top", n - 1)),
            "cache_bytes": CACHE.size,
            "scope": "real 1h tiny-video, 108000 HDF index rows; OS cache not flushed; not D405 performance",
        }
    result["peak_rss_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
