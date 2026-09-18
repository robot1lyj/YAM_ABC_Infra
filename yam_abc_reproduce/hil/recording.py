"""Bounded streaming HIL recording. Video encoding never runs in the control owner."""

from __future__ import annotations

import queue
import shutil
import threading
import time
from pathlib import Path

import numpy as np

from ..resource_qos import place_on_cpus

SAVE_STALL_SECONDS = 120


def wait_for_save(thread, progress_at, label):
    """Allow an arbitrarily long drain while writes advance; detect a stuck writer."""
    observed = progress_at()
    idle_since = time.monotonic()
    while thread.is_alive():
        thread.join(0.5)
        current = progress_at()
        if current > observed:
            observed = current
            idle_since = time.monotonic()
        elif thread.is_alive() and time.monotonic() - idle_since > SAVE_STALL_SECONDS:
            raise RuntimeError(f"{label} made no save progress for {SAVE_STALL_SECONDS} seconds")


def json_value(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(type(value).__name__)


class Recorder:
    def __init__(
        self,
        path,
        *,
        fps=30,
        capacity=32,
        metadata=None,
        segment_seconds=60,
        min_free_bytes=512 * 1024**2,
        video_backend=None,
    ):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=False)
        self.fps = fps
        self.segment_seconds = segment_seconds
        self.min_free_bytes = min_free_bytes
        self.metadata = metadata or {}
        self.video_backend = video_backend
        self.queue = queue.Queue(maxsize=capacity)
        self.error = None
        self.written = 0
        self.metrics = {"queue_peak": 0, "bridge_max_ms": 0}
        self.outcome = "unknown"
        self._stop = threading.Event()
        self._bridge_progress_at = time.monotonic()
        self._encoder = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="hil-recorder")
        self._thread.start()

    def save_progress_at(self):
        encoder = self._encoder
        return max(
            self._bridge_progress_at,
            encoder.last_progress_at.value if encoder is not None else 0,
        )

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
        from .recording_process import EncoderProcess
        from .storage import SegmentWriter

        encoder = None
        pending = []
        try:
            place_on_cpus("RECORDING")
            while not self._stop.is_set() or not self.queue.empty():
                try:
                    record, images = self.queue.get(timeout=0.05)
                except queue.Empty:
                    if encoder and not encoder.process.is_alive():
                        raise RuntimeError(encoder.failure())
                    continue
                started = time.monotonic()
                if encoder is None and images:
                    encoder = EncoderProcess(
                        self.path,
                        self.fps,
                        self.metadata,
                        self.segment_seconds,
                        self.min_free_bytes,
                        self.video_backend,
                    )
                    self._encoder = encoder
                    for old in pending:
                        encoder.submit(old, {})
                    pending.clear()
                if encoder:
                    encoder.submit(record, images)
                    self.written = encoder.written.value
                    self.metrics["write_max_ms"] = encoder.write_max_ms.value
                    self.metrics["spool_bytes"] = encoder.spool_bytes.value
                    self.metrics["spool_peak_bytes"] = encoder.spool_peak_bytes
                else:
                    # Startup without images must remain bounded too.
                    if len(pending) >= self.queue.maxsize:
                        raise RuntimeError("no camera images available for recording")
                    pending.append(record)
                self.metrics["queue_peak"] = max(self.metrics["queue_peak"], self.queue.qsize())
                self.metrics["bridge_max_ms"] = max(
                    self.metrics["bridge_max_ms"], (time.monotonic() - started) * 1000
                )
                self._bridge_progress_at = time.monotonic()
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                if encoder:
                    result = encoder.close("aborted" if self.error else self.outcome, self.metadata)
                    self._bridge_progress_at = time.monotonic()
                    self.written = encoder.written.value
                    self.metrics["write_max_ms"] = encoder.write_max_ms.value
                    self.metrics["spool_bytes"] = encoder.spool_bytes.value
                    self.metrics["spool_peak_bytes"] = encoder.spool_peak_bytes
                    self.metrics.update(result)
                else:
                    writer = SegmentWriter(
                        self.path,
                        self.fps,
                        self.metadata,
                        self.segment_seconds,
                        self.min_free_bytes,
                        video_backend=self.video_backend,
                    )
                    for row in pending:
                        writer.append(row, {})
                    writer.close("aborted" if self.error else self.outcome)
                    self.written = writer.written
            except Exception as exc:
                self.error = self.error or f"encoder finalize: {exc}"

    def close(self, outcome="unknown"):
        self.outcome = outcome
        self._stop.set()
        wait_for_save(self._thread, self.save_progress_at, "episode recorder")


class RecordingSession:
    """Ordered episode boundaries; all directory creation and finalization are off control.

    One bounded queue feeds the writer. Every mode is opt-in: the runtime opens a
    non-collection episode only when motion actually starts, while collection uses
    its explicit record command. A mode change closes any preceding episode.
    """

    def __init__(
        self,
        path,
        *,
        mode="hil",
        fps=30,
        capacity=None,
        metadata=None,
        segment_seconds=60,
        min_free_bytes=512 * 1024**2,
        video_backend=None,
    ):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=False)
        self.metadata = metadata or {}
        self.min_free_bytes = min_free_bytes
        self.segment_seconds = segment_seconds
        self.fps = fps
        self.video_backend = video_backend
        # The bounded RAM queue absorbs encoder startup and short segment-close
        # stalls. Ten seconds is a buffer, not a claim that a writer slower than
        # acquisition can keep up for an arbitrarily long episode.
        capacity = max(32, int(np.ceil(fps * 10))) if capacity is None else capacity
        self.queue = queue.Queue(maxsize=capacity)
        self.error = None
        self.queue_peak = 0
        self._completed_steps = 0
        self._active = None
        self.mode = mode
        self.outcome = "unknown"
        self.recording = False
        self._abort_requested = threading.Event()
        self.episodes = []
        self._stop = threading.Event()
        self._session_progress_at = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True, name="episode-session")
        self._write_manifest()
        self._thread.start()

    @property
    def saving(self):
        return not self.recording and (self._active is not None or not self.queue.empty())

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

    def save_progress_at(self):
        active = self._active
        return max(
            self._session_progress_at,
            active.save_progress_at() if active is not None else 0,
        )

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
        if not self.recording and self._put(("start", self.mode, dict(self.metadata))):
            self.recording = True

    def stop_episode(self, outcome="unknown"):
        if self.recording and self._put(("stop", outcome)):
            self.recording = False

    def abort_episode(self):
        """Stop accepting frames now; let the writer drain/abort off control."""
        self.recording = False
        self._abort_requested.set()

    def set_mode(self, mode, outcome="unknown"):
        if mode != self.mode:
            self.stop_episode(outcome)
            self.mode = mode

    def bind_task(self, path, metadata):
        """Promote an unrecorded standalone session without rebuilding the arms."""
        if (
            self.recording
            or self.episodes
            or self._active is not None
            or not self.queue.empty()
            or self.error
            or self._stop.is_set()
        ):
            raise ValueError("当前会话已有录制数据，不能更换采集任务")
        target = Path(path)
        if target.exists():
            raise FileExistsError(f"采集会话目录已存在：{target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        original = self.path
        original.rename(target)
        try:
            from .storage import atomic_json

            atomic_json(
                target / "session.json",
                dict(
                    metadata,
                    schema="yam_session_v2",
                    episodes=[],
                    session_queue_peak=self.queue_peak,
                    error=None,
                    outcome=self.outcome,
                ),
            )
        except Exception:
            target.rename(original)
            raise
        self.path = target
        self.metadata = metadata

    def submit(self, record, images):
        if not self.recording:
            return True
        if self.error:
            return False
        # Retain gaps and HOLD in active episodes for audit; exporter selects experts.
        return self._put(("row", record, images))

    def _run(self):
        active = None
        active_metadata = {}
        count = 0

        def finish(outcome):
            nonlocal active
            if active is None:
                return
            active.metadata.update(self.metadata)
            active.metadata.update(active_metadata)
            active.close(outcome)
            self._completed_steps += active.written
            self._session_progress_at = time.monotonic()
            self._active = None
            entry = {
                "path": active.path.name,
                "steps": active.written,
                "outcome": "aborted" if active.error else outcome,
            }
            # Publish the in-memory checkpoint only after its atomic on-disk
            # manifest is visible. Readers use ``episodes`` as the completion
            # signal and must never observe it ahead of ``session.json``.
            pending_episodes = [*self.episodes, entry]
            self._write_manifest(episodes=pending_episodes)
            self.episodes.append(entry)
            if active.error:
                raise RuntimeError(active.error)
            if outcome == "discarded":
                shutil.rmtree(active.path)
            active = None

        try:
            place_on_cpus("RECORDING")
            while not self._stop.is_set() or not self.queue.empty():
                try:
                    item = self.queue.get(timeout=0.05)
                except queue.Empty:
                    if active and active.error:
                        raise RuntimeError(active.error)
                    if self._abort_requested.is_set():
                        self._abort_requested.clear()
                        finish("aborted")
                    continue
                if item[0] == "start":
                    if active:
                        raise RuntimeError("episode already open")
                    count += 1
                    active_metadata = dict(item[2], collection_mode=item[1])
                    active = Recorder(
                        self.path / f"episode_{count:06d}",
                        fps=self.fps,
                        segment_seconds=self.segment_seconds,
                        min_free_bytes=self.min_free_bytes,
                        metadata=dict(active_metadata),
                        video_backend=self.video_backend,
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
                self._session_progress_at = time.monotonic()
            finish("aborted" if self.error else self.outcome)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            if active:
                active.metadata.update(self.metadata)
                active.metadata.update(active_metadata)
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

    def _write_manifest(self, *, episodes=None):
        try:
            from .storage import atomic_json

            atomic_json(
                self.path / "session.json",
                dict(
                    self.metadata,
                    schema="yam_session_v2",
                    episodes=self.episodes if episodes is None else episodes,
                    session_queue_peak=self.queue_peak,
                    error=self.error,
                    outcome="aborted" if self.error else self.outcome,
                ),
            )

        except OSError as exc:
            self.error = f"session manifest write: {exc}"

    def close(self, outcome="unknown"):
        self.outcome = outcome
        self.recording = False
        self._stop.set()
        wait_for_save(self._thread, self.save_progress_at, "recording session")
