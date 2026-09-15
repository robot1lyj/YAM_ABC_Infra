"""Spawned encoder with a fixed shared RGB ring; only small rows cross IPC."""

import ctypes
import multiprocessing as mp
import os
import queue
import signal
import time
import traceback

import numpy as np

from .storage import SegmentWriter


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
    buffers,
    shapes,
    incoming,
    free,
    result,
    written,
    write_max_ms,
    video_backend,
):
    writer = None
    try:
        parent_death_guard()
        writer = SegmentWriter(
            path, fps, metadata, seconds, reserve, video_backend=video_backend
        )
        views = {r: np.frombuffer(b, dtype=np.uint8).reshape(shapes[r]) for r, b in buffers.items()}
        while True:
            item = incoming.get()
            if item[0] == "close":
                writer.close(item[1], item[2])
                result.put({"error": None, "segments": len(writer.segments)})
                return
            _, slot, row, roles = item
            try:
                started = time.monotonic()
                writer.append(row, {r: views[r][slot] for r in roles})
                write_max_ms.value = max(write_max_ms.value, (time.monotonic() - started) * 1000)
                written.value = writer.written
            finally:
                free.put(slot)
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
    def __init__(
        self, path, fps, metadata, images, capacity, seconds, reserve, video_backend=None
    ):
        ctx = mp.get_context("spawn")
        self.incoming, self.free, self.result = (
            ctx.Queue(capacity),
            ctx.Queue(capacity),
            ctx.Queue(1),
        )
        self.written = ctx.Value("q", 0)
        self.write_max_ms = ctx.Value("d", 0)
        shapes = {r: (capacity, *im.shape) for r, im in images.items()}
        self.buffers = {r: ctx.RawArray("B", int(np.prod(shape))) for r, shape in shapes.items()}
        self.views = {
            r: np.frombuffer(b, dtype=np.uint8).reshape(shapes[r]) for r, b in self.buffers.items()
        }
        for i in range(capacity):
            self.free.put(i)
        self.process = ctx.Process(
            target=encode,
            args=(
                str(path),
                fps,
                metadata,
                seconds,
                reserve,
                self.buffers,
                shapes,
                self.incoming,
                self.free,
                self.result,
                self.written,
                self.write_max_ms,
                video_backend,
            ),
            daemon=True,
        )
        self.process.start()

    def submit(self, row, images):
        while self.process.is_alive():
            try:
                slot = self.free.get(timeout=0.1)
                break
            except queue.Empty:
                continue
        else:
            raise RuntimeError(self.failure())
        try:
            for role, im in images.items():
                if role not in self.views or self.views[role][slot].shape != im.shape:
                    raise ValueError("camera layout changed after encoder startup")
                np.copyto(self.views[role][slot], im, casting="no")
            self.incoming.put(("row", slot, row, tuple(images)), timeout=1)
        except BaseException:
            self.free.put(slot)
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
            deadline = time.monotonic() + 25
            while True:
                try:
                    self.incoming.put(("close", outcome, metadata), timeout=0.1)
                    break
                except queue.Full:
                    if not self.process.is_alive() or time.monotonic() > deadline:
                        raise RuntimeError("encoder close queue stalled")
            self.process.join(max(0, deadline - time.monotonic()))
            if self.process.is_alive():
                raise RuntimeError("encoder close timed out; unfinished segment retained")
            status = self.result.get(timeout=1)
            if status["error"]:
                raise RuntimeError(status["error"])
            return status
        finally:
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(2)
            for q in (self.incoming, self.free, self.result):
                q.cancel_join_thread()
                q.close()
