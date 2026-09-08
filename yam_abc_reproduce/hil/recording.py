"""Bounded streaming HIL recording. Video encoding never runs in the control owner."""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path

import numpy as np


def json_value(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(type(value).__name__)


class Recorder:
    def __init__(self, path, *, fps=30, capacity=8, metadata=None):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=False)
        self.fps = fps
        self.metadata = metadata or {}
        self.queue = queue.Queue(maxsize=capacity)
        self.error = None
        self.written = 0
        self.outcome = "unknown"
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="hil-recorder")
        self._thread.start()

    def submit(self, record, images):
        # Arrays are owned immutable camera snapshots. No large copies here.
        if self.error or self._stop.is_set():
            return False
        try:
            self.queue.put_nowait((record, images))
            return True
        except queue.Full:
            self.error = "recording queue full"
            return False

    def _run(self):
        import av

        videos = {}
        counts = {}
        try:
            with (self.path / "steps.jsonl").open("w") as log:
                while not self._stop.is_set() or not self.queue.empty():
                    try:
                        record, images = self.queue.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    indices = {}
                    for role, im in images.items():
                        if role not in ("top", "left", "right"):
                            raise ValueError("unknown image role")
                        if role not in videos:
                            # Fragmented MP4 retains earlier fragments after interruption.
                            container = av.open(
                                str(self.path / f"{role}.mp4"),
                                "w",
                                options={"movflags": "frag_keyframe+empty_moov+default_base_moof"},
                            )
                            stream = container.add_stream(
                                "libx264",
                                rate=int(self.fps),
                                options={"preset": "ultrafast", "crf": "20", "tune": "zerolatency"},
                            )
                            stream.width, stream.height = im.shape[1], im.shape[0]
                            stream.pix_fmt = "yuv420p"
                            stream.gop_size = int(self.fps)
                            videos[role] = container, stream
                            counts[role] = 0
                        container, stream = videos[role]
                        for packet in stream.encode(av.VideoFrame.from_ndarray(im, format="rgb24")):
                            container.mux(packet)
                        indices[role] = counts[role]
                        counts[role] += 1
                    record = dict(record, video_indices=indices)
                    log.write(json.dumps(record, default=json_value, allow_nan=False) + "\n")
                    log.flush()
                    self.written += 1
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            for container, stream in videos.values():
                try:
                    for packet in stream.encode():
                        container.mux(packet)
                    container.close()
                except Exception as exc:
                    self.error = f"video close: {exc}"
            manifest = dict(
                self.metadata,
                schema="yam_hil_v1",
                steps=self.written,
                video_frames=counts,
                fps=self.fps,
                error=self.error,
                outcome="aborted" if self.error else self.outcome,
                clock="RK host monotonic; camera arrival alignment",
                action_semantics="submitted command is not measured motion",
            )
            (self.path / "manifest.json").write_text(
                json.dumps(manifest, default=json_value, indent=2) + "\n"
            )

    def close(self, outcome="unknown"):
        self.outcome = outcome
        self._stop.set()
        self._thread.join(30)
        if self._thread.is_alive():
            raise RuntimeError("recorder did not finish within 30 seconds")
