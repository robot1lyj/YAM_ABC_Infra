"""CameraWorker: run a CameraDriver in a background thread."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable

from ..resource_qos import place_on_cpus
from .interface import CameraDriver, CameraFrame, CameraMode


class CameraWorker:
    """Wrap a ``CameraDriver`` so ``read()`` returns the latest frame without
    blocking. Exposes the driver's name/role/mode/image_keys so the recorder and
    GUI treat it like a driver.

    An optional ``on_frame`` callback is invoked with ``(name, frame)`` on every
    capture, so previews update at the camera's full frame rate independently of
    the control loop (the loop can be stopped and previews still stream)."""

    def __init__(
        self,
        driver: CameraDriver,
        on_frame: Callable[[str, CameraFrame], None] | None = None,
    ):
        self._driver = driver
        self._on_frame = on_frame
        self.name: str = driver.name
        self.role: str = driver.role
        self.mode: CameraMode = driver.mode
        self._history = deque(maxlen=8)
        self._sequence = 0
        self._latest: CameraFrame | None = None
        self._lock = threading.Lock()
        self._first = threading.Event()
        self._startup_error: Exception | None = None
        self._running = False
        self._thread: threading.Thread | None = None

    def image_keys(self) -> list[str]:
        return self._driver.image_keys()

    def start(self, warmup_timeout: float = 5.0) -> None:
        """Start capturing and block until the first frame arrives, so callers are
        guaranteed a valid frame afterwards. Raises if none arrives in time."""
        if self._running:
            return
        self._running = True
        self._first.clear()
        self._startup_error = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._first.wait(warmup_timeout):
            self.stop()
            raise RuntimeError(f"camera {self.name!r} produced no frames within {warmup_timeout}s")
        if self._startup_error is not None:
            error = self._startup_error
            self.stop()
            raise RuntimeError(f"camera {self.name!r} CPU placement failed: {error}") from error

    def _run(self) -> None:
        try:
            place_on_cpus("CAMERA")
        except Exception as exc:
            self._startup_error = exc
            self._first.set()
            return
        while self._running:
            try:
                frame = self._driver.read()
            except Exception:
                # Keep the last good frame; a transient read error shouldn't kill
                # capture. A camera that never produces trips the warmup timeout.
                time.sleep(0.01)
                continue
            self._sequence += 1
            frame.meta.setdefault("host_received_at", time.monotonic())
            frame.meta["sequence"] = self._sequence
            with self._lock:
                self._latest = frame
                self._history.append(frame)
            if self._on_frame is not None:
                self._on_frame(self.name, frame)  # feed preview at camera fps
            self._first.set()
            # Synthetic drivers do not block on exposure.
            if self._driver.__class__.__name__ == "MockCamera":
                time.sleep(1 / 30)

    def read(self) -> CameraFrame | None:
        """Latest captured frame (never None once started + warmed up)."""
        with self._lock:
            return self._latest

    def history(self) -> list[CameraFrame]:
        with self._lock:
            return list(self._history)

    def stop(self, join_timeout: float = 6.0) -> None:
        # Join long enough for an in-flight blocking read to return (RealSense
        # wait_for_frames can take ~5s) before stopping the driver — overlapping
        # driver.stop() with a live read can crash librealsense. If the reader is
        # still stuck after that, retain it for an explicit stop retry rather
        # than overlapping driver.stop() with a live read.
        self._running = False
        if self._thread is not None:
            # Thread.start can fail before a native thread exists. The driver
            # still needs closing; joining that unstarted thread would prevent it.
            if self._thread.ident is not None:
                self._thread.join(timeout=join_timeout)
            if self._thread.is_alive():
                logging.warning(
                    "camera %r reader still running after %.0fs; skipping driver.stop()",
                    self.name,
                    join_timeout,
                )
                raise RuntimeError(f"camera {self.name!r} reader has not stopped; retry close")
            self._thread = None
        self._driver.stop()
