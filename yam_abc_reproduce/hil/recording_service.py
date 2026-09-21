"""Separate recording owner; the control loop only enqueues immutable snapshots.

The transport thread, not the control thread, serializes RGB and talks to the
recording process. A recording failure is reported to Runtime for its existing
HOLD path; it never terminates the hardware owner process.
"""

from __future__ import annotations

import multiprocessing as mp
import queue
import threading
import time
from pathlib import Path

import numpy as np

from ..resource_qos import place_on_cpus
from .recording import RecordingSession, wait_for_save
from .recording_process import parent_death_guard


def _serve_recording(connection, ready, progress, options):
    recorder = None
    done = threading.Event()

    def report_progress():
        while not done.wait(0.25):
            if recorder is not None:
                progress.value = recorder.save_progress_at()

    try:
        parent_death_guard()
        place_on_cpus("RECORDING")
        recorder = RecordingSession(**options)
        monitor = threading.Thread(target=report_progress, daemon=True, name="record-progress")
        monitor.start()
        ready.send({"error": None})
        ready.close()
        while True:
            kind, args = connection.recv()
            error = None
            try:
                if kind == "row":
                    if not recorder.submit(*args):
                        error = recorder.error or "recording receiver unavailable"
                elif kind == "start":
                    recorder.metadata.update(args[0])
                    recorder.start_episode()
                elif kind == "stop":
                    recorder.metadata.update(args[1])
                    recorder.stop_episode(args[0])
                elif kind == "abort":
                    recorder.abort_episode()
                elif kind == "mode":
                    recorder.set_mode(*args)
                elif kind == "bind":
                    recorder.bind_task(*args)
                elif kind == "rotate":
                    recorder.rotate_task(*args)
                elif kind == "status":
                    pass
                elif kind == "close":
                    recorder.metadata.update(args[1])
                    recorder.close(args[0])
                else:
                    raise ValueError(f"unknown recorder command: {kind}")
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            progress.value = recorder.save_progress_at()
            connection.send(
                {
                    "error": recorder.error if kind == "rotate" else error or recorder.error,
                    "command_error": error if kind == "rotate" else None,
                    "recording": recorder.recording,
                    "saving": recorder.saving,
                    "written": recorder.written,
                    "episodes": list(recorder.episodes),
                    "metrics": recorder.metrics,
                    "queue": recorder.queue.qsize(),
                    "path": str(recorder.path),
                }
            )
            if kind == "close":
                return
    except BaseException as exc:
        try:
            ready.send({"error": f"{type(exc).__name__}: {exc}"})
        except (BrokenPipeError, OSError):
            pass
    finally:
        done.set()
        if recorder is not None and recorder._thread.is_alive():
            recorder.abort_episode()
            try:
                recorder.close("aborted")
            except Exception:
                pass
        connection.close()
        ready.close()


class RemoteRecordingSession:
    """RecordingSession-compatible facade with a bounded, nonblocking submit."""

    def __init__(
        self,
        path,
        *,
        mode="hil",
        fps=30,
        metadata=None,
        segment_seconds=60,
        min_free_bytes=512 * 1024**2,
        video_backend=None,
        capacity=None,
    ):
        self.path = Path(path)
        self.mode = mode
        self.metadata = metadata or {}
        self.recording = False
        self.outcome = "unknown"
        self.written = 0
        self.episodes = []
        self.metrics = {}
        self._remote_saving = False
        self._remote_queue = 0
        self._queue_peak = 0
        self._transport_max_ms = 0.0
        self._error = None
        self._closed = False
        self.queue = queue.Queue(
            maxsize=max(32, int(np.ceil(fps * 10))) if capacity is None else capacity
        )
        context = mp.get_context("spawn")
        parent, child = context.Pipe()
        ready_parent, ready_child = context.Pipe(duplex=False)
        self._connection = parent
        self._progress = context.Value("d", time.monotonic())
        self.process = context.Process(
            target=_serve_recording,
            args=(
                child,
                ready_child,
                self._progress,
                dict(
                    path=self.path,
                    mode=mode,
                    fps=fps,
                    metadata=dict(self.metadata),
                    segment_seconds=segment_seconds,
                    min_free_bytes=min_free_bytes,
                    video_backend=video_backend,
                ),
            ),
            name="yam-recording-owner",
        )
        self.process.start()
        child.close()
        ready_child.close()
        try:
            if not ready_parent.poll(10):
                raise RuntimeError("recording process did not become ready")
            result = ready_parent.recv()
            if result["error"]:
                raise RuntimeError(result["error"])
        except BaseException:
            self.process.terminate()
            self.process.join(2)
            parent.close()
            raise
        finally:
            ready_parent.close()
        self._thread = threading.Thread(target=self._send, daemon=True, name="record-transport")
        self._thread.start()

    @property
    def error(self):
        if hasattr(self, "_thread") and not self._closed and not self._thread.is_alive():
            return self._error or "recording transport stopped"
        if not self._closed and not self.process.is_alive():
            return self._error or f"recording process exited: {self.process.exitcode}"
        return self._error

    @property
    def saving(self):
        return not self.recording and (self._remote_saving or not self.queue.empty())

    @property
    def queue_depth(self):
        return self.queue.qsize() + self._remote_queue

    def save_progress_at(self):
        return self._progress.value

    def _enqueue(self, kind, *args, completion=None):
        if (self.error and kind not in ("abort", "close")) or self._closed:
            return False
        try:
            self.queue.put_nowait((kind, args, completion))
            self._queue_peak = max(self._queue_peak, self.queue.qsize())
            return True
        except queue.Full:
            self._error = "episode queue full"
            return False

    def _send(self):
        try:
            place_on_cpus("RECORDING")
        except Exception as exc:
            self._error = f"recording transport CPU placement failed: {exc}"
            return
        while True:
            try:
                kind, args, completion = self.queue.get(timeout=0.1)
            except queue.Empty:
                if not self.process.is_alive() and not self._closed:
                    self._error = self.error
                    return
                kind, args, completion = "status", (), None
            try:
                started = time.monotonic()
                self._connection.send((kind, args))
                state = self._connection.recv()
                self._transport_max_ms = max(
                    self._transport_max_ms, (time.monotonic() - started) * 1000
                )
                self._error = state["error"] or self._error
                self._remote_saving = state["saving"]
                self.written = state["written"]
                self.episodes = state["episodes"]
                self.metrics = {
                    **state["metrics"],
                    "recording_pid": self.process.pid,
                    "transport_queue_peak": self._queue_peak,
                    "transport_max_ms": self._transport_max_ms,
                }
                self._remote_queue = state["queue"]
                self.path = Path(state["path"])
                if completion is not None:
                    completion["result"] = state
            except (BrokenPipeError, EOFError, OSError) as exc:
                self._error = f"recording process unavailable: {exc}"
                if completion is not None:
                    completion["error"] = self._error
                return
            finally:
                if completion is not None:
                    completion["event"].set()
            if kind == "close":
                return

    def _request(self, kind, *args, allow_error=False):
        completion = {"event": threading.Event()}
        observed = self.save_progress_at()
        idle_since = time.monotonic()
        if kind == "close":
            while True:
                if not self._thread.is_alive():
                    raise RuntimeError(self.error or "recording transport stopped")
                try:
                    self.queue.put((kind, args, completion), timeout=0.2)
                    break
                except queue.Full:
                    current = self.save_progress_at()
                    if current > observed:
                        observed = current
                        idle_since = time.monotonic()
                    elif time.monotonic() - idle_since > 120:
                        raise RuntimeError("recording transport made no progress for 120 seconds")
        elif not self._enqueue(kind, *args, completion=completion):
            raise RuntimeError(self.error or "recorder command queue full")
        while not completion["event"].wait(0.2):
            if not self._thread.is_alive():
                raise RuntimeError(self.error or "recording transport stopped")
            if kind == "close":
                current = self.save_progress_at()
                if current > observed:
                    observed = current
                    idle_since = time.monotonic()
                elif time.monotonic() - idle_since > 120:
                    raise RuntimeError("recording process made no save progress for 120 seconds")
        if completion.get("error"):
            raise RuntimeError(completion["error"])
        result = completion["result"]
        if result.get("command_error"):
            raise RuntimeError(result["command_error"])
        if result["error"] and not allow_error:
            raise RuntimeError(result["error"])
        return result

    def start_episode(self):
        if not self.recording and self._enqueue("start", dict(self.metadata)):
            self.recording = True

    def stop_episode(self, outcome="unknown"):
        if self.recording and self._enqueue("stop", outcome, dict(self.metadata)):
            self.recording = False

    def abort_episode(self):
        self.recording = False
        self._enqueue("abort")

    def set_mode(self, mode, outcome="unknown"):
        if mode != self.mode:
            self.stop_episode(outcome)
            if self._enqueue("mode", mode, outcome):
                self.mode = mode

    def submit(self, record, images):
        if not self.recording:
            return True
        return self._enqueue("row", record, images)

    def bind_task(self, path, metadata):
        self._request("bind", str(path), metadata)
        self.metadata = metadata

    def rotate_task(self, path, metadata):
        self._request("rotate", str(path), metadata)
        self.metadata = metadata

    def close(self, outcome="unknown"):
        if self._closed:
            return
        self.outcome = outcome
        self.recording = False
        try:
            self._request("close", outcome, dict(self.metadata), allow_error=True)
            wait_for_save(self._thread, self.save_progress_at, "recording transport")
            self.process.join(2)
        finally:
            self._closed = True
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(2)
            self._connection.close()
