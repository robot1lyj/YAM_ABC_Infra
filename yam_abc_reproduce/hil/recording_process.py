"""Spawned encoder fed by an on-disk overflow spool.

The recording bridge writes each immutable row to NVMe before notifying the
encoder process. The encoder consumes files in order and deletes a segment's
source files only after its MP4/HDF5 segment is committed. Thus a temporary
encoder deficit grows on disk, not in the control process's RAM.
"""

import ctypes
import multiprocessing as mp
import os
import pickle
import queue
import signal
import shutil
import time
import traceback
from pathlib import Path

from ..resource_qos import place_on_cpus
from .storage import SegmentWriter

SAVE_STALL_SECONDS = 120


def parent_death_guard():
    """Linux: do not leave a writer/converter orphaned after its owner is killed."""
    parent = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot set parent-death signal")
    if parent == 1 or os.getppid() != parent:
        os.kill(os.getpid(), signal.SIGKILL)


def encode(
    path,
    fps,
    metadata,
    seconds,
    reserve,
    spool,
    incoming,
    result,
    written,
    write_max_ms,
    last_progress_at,
    spool_bytes,
    video_backend,
):
    writer = None
    pending_segment = []
    try:
        parent_death_guard()
        # Three FFmpeg/MPP subprocesses inherit this writer process's CPU mask.
        place_on_cpus("ENCODER")
        writer = SegmentWriter(path, fps, metadata, seconds, reserve, video_backend=video_backend)

        def clear_committed_segment():
            for source, size in pending_segment:
                source.unlink()
                with spool_bytes.get_lock():
                    spool_bytes.value = max(0, spool_bytes.value - size)
            pending_segment.clear()

        while True:
            item = incoming.get()
            if item[0] == "close":
                writer.close(item[1], item[2])
                clear_committed_segment()
                last_progress_at.value = time.monotonic()
                result.put({"error": None, "segments": len(writer.segments)})
                return
            _, filename = item
            source = Path(spool) / filename
            size = source.stat().st_size
            with source.open("rb") as stream:
                row, images = pickle.load(stream)
            started = time.monotonic()
            committed = len(writer.segments)
            writer.append(row, images)
            write_max_ms.value = max(write_max_ms.value, (time.monotonic() - started) * 1000)
            written.value = writer.written
            last_progress_at.value = time.monotonic()
            pending_segment.append((source, size))
            if len(writer.segments) > committed:
                clear_committed_segment()
    except BaseException:
        error = traceback.format_exc(limit=4)
        if writer:
            writer.error = error
            try:
                writer.close("aborted")
            except Exception:
                pass
        result.put({"error": error})


class EncoderProcess:
    def __init__(self, path, fps, metadata, seconds, reserve, video_backend=None):
        ctx = mp.get_context("spawn")
        self.incoming, self.result = ctx.Queue(), ctx.Queue(1)
        self.written = ctx.Value("q", 0)
        self.write_max_ms = ctx.Value("d", 0)
        self.last_progress_at = ctx.Value("d", time.monotonic())
        self.spool_bytes = ctx.Value("q", 0)
        self.spool_peak_bytes = 0
        self.reserve = int(reserve)
        self.spool = Path(path) / ".recording-spool"
        self.spool.mkdir(exist_ok=False)
        self.sequence = 0
        self.process = ctx.Process(
            target=encode,
            args=(
                str(path),
                fps,
                metadata,
                seconds,
                reserve,
                str(self.spool),
                self.incoming,
                self.result,
                self.written,
                self.write_max_ms,
                self.last_progress_at,
                self.spool_bytes,
                video_backend,
            ),
            daemon=True,
        )
        self.process.start()

    def submit(self, row, images):
        if not self.process.is_alive():
            raise RuntimeError(self.failure())
        # Check the filesystem that actually carries the temporary backlog.
        # The normal episode reserve therefore also bounds the spool.
        image_bytes = sum(image.nbytes for image in images.values())
        if shutil.disk_usage(self.spool).free < self.reserve + image_bytes + 1024 * 1024:
            raise OSError("recording stopped: low disk space for encoder backlog")
        filename = f"{self.sequence:012d}.pkl"
        self.sequence += 1
        target = self.spool / filename
        temporary = target.with_suffix(".tmp")
        accounted = 0
        try:
            with temporary.open("wb", buffering=1024 * 1024) as stream:
                pickle.dump((row, images), stream, protocol=pickle.HIGHEST_PROTOCOL)
            temporary.rename(target)
            size = target.stat().st_size
            with self.spool_bytes.get_lock():
                self.spool_bytes.value += size
                accounted = size
                self.spool_peak_bytes = max(self.spool_peak_bytes, self.spool_bytes.value)
            self.incoming.put(("row", filename))
        except BaseException:
            temporary.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            if accounted:
                with self.spool_bytes.get_lock():
                    self.spool_bytes.value = max(0, self.spool_bytes.value - accounted)
            raise

    def failure(self):
        try:
            return self.result.get(timeout=0.5).get("error") or "encoder exited unexpectedly"
        except queue.Empty:
            return f"encoder exited: {self.process.exitcode}"

    def close(self, outcome, metadata):
        try:
            if not self.process.is_alive():
                raise RuntimeError(self.failure())
            observed = self.last_progress_at.value
            idle_since = time.monotonic()

            def check_progress():
                nonlocal observed, idle_since
                current = self.last_progress_at.value
                if current > observed:
                    observed = current
                    idle_since = time.monotonic()
                elif time.monotonic() - idle_since > SAVE_STALL_SECONDS:
                    raise RuntimeError(
                        f"encoder made no save progress for {SAVE_STALL_SECONDS} seconds"
                    )

            while True:
                try:
                    self.incoming.put(("close", outcome, metadata), timeout=0.1)
                    break
                except queue.Full:
                    if not self.process.is_alive():
                        raise RuntimeError(self.failure())
                    check_progress()
            while self.process.is_alive():
                self.process.join(0.5)
                if self.process.is_alive():
                    check_progress()
            status = self.result.get(timeout=1)
            if status["error"]:
                raise RuntimeError(status["error"])
            status["spool_peak_bytes"] = self.spool_peak_bytes
            return status
        finally:
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(2)
            for q in (self.incoming, self.result):
                q.cancel_join_thread()
                q.close()
            if self.spool.exists() and not any(self.spool.iterdir()):
                self.spool.rmdir()
