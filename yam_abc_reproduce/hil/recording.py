"""Bounded streaming HIL recording. Video encoding never runs in the control owner."""

from __future__ import annotations

import json
import queue
import shutil
import threading
import time
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
        self.metrics = {"queue_peak": 0, "encode_max_ms": 0}
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
        videos = {}
        counts = {}
        try:
            import av

            with (self.path / "steps.jsonl").open("w") as log:
                while not self._stop.is_set() or not self.queue.empty():
                    try:
                        record, images = self.queue.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    encode_started = time.monotonic()
                    self.metrics["queue_peak"] = max(self.metrics["queue_peak"], self.queue.qsize())
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
                            stream.thread_count = 1
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
                    self.metrics["encode_max_ms"] = max(
                        self.metrics["encode_max_ms"], (time.monotonic() - encode_started) * 1000
                    )
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
                recording_metrics=self.metrics,
                video_frames=counts,
                fps=self.fps,
                error=self.error,
                outcome="aborted" if self.error else self.outcome,
                clock="RK host monotonic; camera arrival alignment",
                action_semantics="submitted command is not measured motion",
            )
            try:
                (self.path / "manifest.json").write_text(
                    json.dumps(manifest, default=json_value, indent=2) + "\n"
                )
            except OSError as exc:
                self.error = f"manifest write: {exc}"

    def close(self, outcome="unknown"):
        self.outcome = outcome
        self._stop.set()
        self._thread.join(30)
        if self._thread.is_alive():
            raise RuntimeError("recorder did not finish within 30 seconds")


class RecordingSession:
    """Ordered episode boundaries; all directory creation and finalization are off control.

    One bounded queue feeds the writer. Collection is opt-in; other modes record
    continuously. A mode change closes the preceding episode before opening another.
    """

    def __init__(self, path, *, mode="hil", fps=30, capacity=32, metadata=None):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=False)
        self.metadata = metadata or {}
        self.fps = fps
        self.queue = queue.Queue(maxsize=capacity)
        self.error = None
        self.queue_peak = 0
        self._completed_steps = 0
        self._active = None
        self.mode = mode
        self.outcome = "unknown"
        self.recording = False
        self.episodes = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="episode-session")
        self._thread.start()
        if mode != "collect":
            self.start_episode()

    @property
    def written(self):
        active = self._active
        return self._completed_steps + (active.written if active else 0)

    @property
    def metrics(self):
        active = self._active
        return {
            "session_queue_peak": self.queue_peak,
            "encoder_queue": active.queue.qsize() if active else 0,
            "encoder": dict(active.metrics) if active else {},
        }

    def _put(self, item):
        if self.error or self._stop.is_set():
            return False
        try:
            self.queue.put_nowait(item)
            self.queue_peak = max(self.queue_peak, self.queue.qsize())
            return True
        except queue.Full:
            self.error = "episode queue full"
            return False

    def start_episode(self):
        if not self.recording and self._put(("start", self.mode)):
            self.recording = True

    def stop_episode(self, outcome="unknown"):
        if self.recording and self._put(("stop", outcome)):
            self.recording = False

    def set_mode(self, mode, outcome="unknown"):
        if mode != self.mode:
            self.stop_episode(outcome)
            self.mode = mode
            if mode != "collect":
                self.start_episode()

    def submit(self, record, images):
        if self.error:
            return False
        if not self.recording:
            return True
        # Retain gaps and HOLD in active episodes for audit; exporter selects experts.
        return self._put(("row", record, images))

    def _run(self):
        active = None
        count = 0

        def finish(outcome):
            nonlocal active
            if active is None:
                return
            active.metadata.update(self.metadata)
            active.close(outcome)
            self._completed_steps += active.written
            self._active = None
            self.episodes.append(
                {
                    "path": active.path.name,
                    "steps": active.written,
                    "outcome": "aborted" if active.error else outcome,
                }
            )
            if active.error:
                raise RuntimeError(active.error)
            if outcome == "discarded":
                shutil.rmtree(active.path)
            active = None

        try:
            while not self._stop.is_set() or not self.queue.empty():
                try:
                    item = self.queue.get(timeout=0.05)
                except queue.Empty:
                    if active and active.error:
                        raise RuntimeError(active.error)
                    continue
                if item[0] == "start":
                    if active:
                        raise RuntimeError("episode already open")
                    count += 1
                    active = Recorder(
                        self.path / f"episode_{count:06d}",
                        fps=self.fps,
                        metadata=dict(self.metadata, collection_mode=item[1]),
                    )
                    self._active = active
                elif item[0] == "stop":
                    finish(item[1])
                elif item[0] == "row":
                    if active is None:
                        raise RuntimeError("episode writer unavailable")
                    while True:
                        if active.error or not active._thread.is_alive():
                            raise RuntimeError(active.error or "episode writer stopped")
                        try:
                            active.queue.put((item[1], item[2]), timeout=0.05)
                            break
                        except queue.Full:
                            continue
            finish("aborted" if self.error else self.outcome)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            if active:
                active.metadata.update(self.metadata)
                try:
                    active.close("aborted")
                except Exception as close_exc:
                    self.error += f"; close: {close_exc}"
                if not any(e["path"] == active.path.name for e in self.episodes):
                    self.episodes.append(
                        {
                            "path": active.path.name,
                            "steps": active.written,
                            "outcome": "aborted",
                            "error": self.error,
                        }
                    )
        finally:
            self._write_manifest()

    def _write_manifest(self):
        try:
            (self.path / "session.json").write_text(
                json.dumps(
                    dict(
                        self.metadata,
                        schema="yam_session_v1",
                        episodes=self.episodes,
                        session_queue_peak=self.queue_peak,
                        error=self.error,
                        outcome="aborted" if self.error else self.outcome,
                    ),
                    default=json_value,
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )

        except OSError as exc:
            self.error = f"session manifest write: {exc}"

    def close(self, outcome="unknown"):
        self.outcome = outcome
        self.recording = False
        self._stop.set()
        self._thread.join(35)
        if self._thread.is_alive():
            raise RuntimeError("session writer did not finish within 35 seconds")
