"""Best-effort 5 Hz previews with one shared latest-frame slot, separate process."""

import multiprocessing as mp
import queue
import time

import numpy as np

SHAPE = (3, 720, 1280, 3)
ROLES = ("top", "left", "right")


def _encode(buffer, dimensions, lock, output, stop):
    import os

    import cv2

    try:
        os.nice(10)
    except OSError:
        pass
    cv2.setNumThreads(1)
    frames = np.frombuffer(buffer, dtype=np.uint8).reshape(SHAPE)
    while not stop.wait(0.2):
        copied = []
        if not lock.acquire(False):
            continue
        try:
            for i, role in enumerate(ROLES):
                h, w = dimensions[i * 2 : i * 2 + 2]
                if h and w:
                    copied.append((role, frames[i, :h, :w].copy()))
        finally:
            lock.release()
        images = {}
        for role, rgb in copied:
            small = cv2.resize(rgb, (480, 360))
            ok, encoded = cv2.imencode(
                ".jpg", cv2.cvtColor(small, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 72]
            )
            if ok:
                images[role] = encoded.tobytes()
        try:
            output.put_nowait((time.monotonic(), images))
        except queue.Full:
            pass  # Preview drops are intentional; never backpressure acquisition.


class Preview:
    def __init__(self):
        ctx = mp.get_context("spawn")
        self.buffer = ctx.RawArray("B", int(np.prod(SHAPE)))
        self.dimensions = ctx.RawArray("i", 6)
        self.lock = ctx.Lock()
        self.output = ctx.Queue(maxsize=1)
        self.stop = ctx.Event()
        self.frames = np.frombuffer(self.buffer, dtype=np.uint8).reshape(SHAPE)
        self.process = ctx.Process(
            target=_encode,
            args=(self.buffer, self.dimensions, self.lock, self.output, self.stop),
            daemon=True,
        )
        self.process.start()

    def submit(self, frames):
        if not self.lock.acquire(False):
            return False
        try:
            for i, role in enumerate(ROLES):
                rgb = frames.get(role)
                self.dimensions[i * 2 : i * 2 + 2] = (0, 0)
                if rgb is None or rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.dtype != np.uint8:
                    continue
                h, w = rgb.shape[:2]
                if h > SHAPE[1] or w > SHAPE[2]:
                    continue
                self.frames[i, :h, :w] = rgb
                self.dimensions[i * 2 : i * 2 + 2] = (h, w)
        finally:
            self.lock.release()
        return True

    def poll(self):
        try:
            return self.output.get_nowait()
        except queue.Empty:
            return None

    def close(self):
        self.stop.set()
        self.process.join(timeout=2)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=1)
        self.output.cancel_join_thread()
        self.output.close()
