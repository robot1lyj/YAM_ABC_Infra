#!/usr/bin/env python3
"""Record and read back a camera-only P2 probe without constructing robots."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import av
import numpy as np

from yam_abc_reproduce.camera.worker import CameraWorker
from yam_abc_reproduce.config import build_station_config, load_yaml
from yam_abc_reproduce.hil.observation import Observations
from yam_abc_reproduce.hil.recording import RecordingSession
from yam_abc_reproduce.hil.recording_service import RemoteRecordingSession
from yam_abc_reproduce.hil.storage import read_rows
from yam_abc_reproduce.hil.video import select_backend
from yam_abc_reproduce.runtime import build_cameras_from_config


def video_frames(episode: Path, role: str) -> int:
    count = 0
    for path in sorted(episode.glob(f"segment_*/{role}.mp4")):
        with av.open(str(path)) as container:
            count += sum(1 for _ in container.decode(video=0))
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station", type=Path, default=Path("configs/station_hil.yaml"))
    parser.add_argument("--cameras", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--warmup-seconds", type=float, default=5.0)
    parser.add_argument("--segment-seconds", type=float, default=60.0)
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--separate-recording", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; choose a new path")
    if args.seconds <= 0 or args.warmup_seconds < 0 or args.segment_seconds <= 0:
        parser.error("seconds/segment-seconds must be positive and warmup must be non-negative")

    cfg = build_station_config(args.station, args.cameras)
    settings = load_yaml(args.station).get("hil", {})
    if {camera.role for camera in cfg.cameras} != {"top", "left", "right"}:
        parser.error("station must configure exactly the top, left, and right camera roles")
    dofs = len(cfg.robot.robots) * (cfg.robot.num_arm_joints + 1)
    if dofs <= 0:
        parser.error("station robot layout must define a positive observation size")

    drivers = build_cameras_from_config(cfg, mock=args.mock)
    workers: list[CameraWorker] = []
    recorder = None
    submitted = 0
    invalid = 0
    first_write_elapsed = None
    record_started = None
    encoder_queue_peak = 0
    transport_queue_peak = 0
    spool_peak_bytes = 0
    try:
        for driver in drivers:
            worker = CameraWorker(driver)
            worker.start()
            workers.append(worker)
        time.sleep(args.warmup_seconds)

        first_frame = workers[0].read()
        if first_frame is None or "rgb" not in first_frame.images:
            raise RuntimeError("camera first frame unavailable for encoder backend probe")
        probe_started = time.monotonic()
        backend = (
            "libx264"
            if args.mock
            else select_backend(first_frame.images["rgb"], int(cfg.control_hz))
        )
        backend_probe_elapsed = time.monotonic() - probe_started

        observations = Observations(
            workers,
            max_age=settings.get("max_frame_age", 0.5),
            max_skew=settings.get("max_frame_skew", 0.12),
            warn_skew=settings.get("warn_frame_skew", 0.04),
        )
        recorder_type = RemoteRecordingSession if args.separate_recording else RecordingSession
        recorder = recorder_type(
            args.output,
            mode="collect",
            fps=cfg.control_hz,
            segment_seconds=args.segment_seconds,
            video_backend=backend,
            metadata={
                "mock": args.mock,
                "camera_only_test": True,
                "station": {"task_name": "P2 camera recording test"},
            },
        )
        recorder.start_episode()
        record_started = time.monotonic()
        deadline = time.monotonic() + args.seconds
        tick = 0
        while time.monotonic() < deadline:
            started = time.monotonic()
            state = np.zeros(dofs, dtype=np.float64)
            observations.add_state(started, state)
            time.sleep(0.001)
            now = time.monotonic()
            observations.add_state(now, state)
            snapshot = observations.snapshot(now, "P2 camera recording test")
            if snapshot is None:
                invalid += 1
            else:
                _, _, _, images, quality = snapshot
                record = {
                    "tick": tick,
                    "time": now,
                    "epoch": 0,
                    "mode": "collect",
                    "phase": "human",
                    "source": "human",
                    "is_intervention": False,
                    "intervention_id": None,
                    "observation_valid": True,
                    "expert_valid": True,
                    "measured_state": state,
                    "observation_state": state,
                    "submitted_action": state,
                    "event_requested_at": None,
                    "transitions": [],
                    "sync": quality,
                }
                if not recorder.submit(record, images):
                    raise RuntimeError(recorder.error or "record submit failed")
                submitted += 1
            if first_write_elapsed is None and recorder.written > 0:
                first_write_elapsed = time.monotonic() - record_started
            encoder_queue_peak = max(encoder_queue_peak, recorder.metrics.get("encoder_queue", 0))
            transport_queue_peak = max(
                transport_queue_peak, recorder.metrics.get("transport_queue_peak", 0)
            )
            spool_peak_bytes = max(
                spool_peak_bytes,
                recorder.metrics.get("encoder", {}).get("spool_peak_bytes", 0),
            )
            tick += 1
            time.sleep(max(0.0, 1 / cfg.control_hz - (time.monotonic() - started)))

        recorder.stop_episode("success")
        save_started = time.monotonic()
        recorder.close("success")
        save_elapsed = time.monotonic() - save_started
        if recorder.error:
            raise RuntimeError(recorder.error)
        if len(recorder.episodes) != 1:
            raise RuntimeError(f"expected one episode, got {len(recorder.episodes)}")

        episode = args.output / recorder.episodes[0]["path"]
        rows = list(read_rows(episode))
        roles = sorted(camera.role for camera in cfg.cameras)
        videos = {role: video_frames(episode, role) for role in roles}
        result = {
            "output": str(args.output.resolve()),
            "mock": args.mock,
            "seconds": args.seconds,
            "warmup_seconds": args.warmup_seconds,
            "segment_seconds": args.segment_seconds,
            "video_backend": backend,
            "backend_probe_elapsed_s": backend_probe_elapsed,
            "first_write_elapsed_s": first_write_elapsed,
            "first_write_poll_resolution_s": 0.05,
            "save_elapsed_s": save_elapsed,
            "encoder_queue_peak": encoder_queue_peak,
            "transport_queue_peak": transport_queue_peak,
            "spool_peak_bytes": spool_peak_bytes,
            "submitted": submitted,
            "invalid_snapshots": invalid,
            "rows_read": len(rows),
            "video_frames": videos,
            "session_queue_peak": recorder.metrics.get(
                "session_queue_peak", getattr(recorder, "queue_peak", 0)
            ),
            "encoder_metrics": recorder.metrics,
            "episode": recorder.episodes[0],
        }
        result["ok"] = (
            submitted > 0
            and len(rows) == submitted
            and all(count == submitted for count in videos.values())
            and recorder.episodes[0]["outcome"] == "success"
        )
        (args.output / "camera_probe.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1
    finally:
        if recorder is not None and recorder._thread.is_alive():
            recorder.close("aborted")
        for worker in workers:
            worker.stop()
        for driver in drivers[len(workers) :]:
            driver.stop()


if __name__ == "__main__":
    raise SystemExit(main())
