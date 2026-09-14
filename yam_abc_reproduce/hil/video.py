"""Video writers for HIL recording, with an RK3588 hardware path."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import numpy as np

RKMPP_FFMPEG = Path("/opt/yam-rkmpp/bin/ffmpeg")
BACKEND_ENV = "YAM_ABC_HIL_VIDEO_ENCODER"


def _command(binary: Path, path: Path, width: int, height: int, fps: int) -> list[str]:
    return [
        str(binary),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pixel_format",
        "rgb24",
        "-video_size",
        f"{width}x{height}",
        "-framerate",
        str(fps),
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "h264_rkmpp",
        "-rc_mode",
        "CQP",
        "-qp_init",
        "20",
        "-g",
        str(fps),
        "-movflags",
        "frag_keyframe+empty_moov+default_base_moof",
        "-y",
        str(path),
    ]


def _rgb_bytes(image: np.ndarray) -> memoryview:
    if not image.flags.c_contiguous:
        image = np.ascontiguousarray(image)
    return memoryview(image).cast("B")


def rkmpp_probe(image: np.ndarray, fps: int, binary: Path = RKMPP_FFMPEG) -> str | None:
    """Return None only after a real one-frame hardware encode succeeds."""
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return f"{binary} is not executable"
    if not os.access("/dev/mpp_service", os.R_OK | os.W_OK):
        return "/dev/mpp_service is not readable and writable"
    with tempfile.TemporaryDirectory(prefix="yam-rkmpp-probe-") as directory:
        output = Path(directory) / "probe.mp4"
        try:
            result = subprocess.run(
                _command(binary, output, image.shape[1], image.shape[0], fps),
                input=_rgb_bytes(image),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"{type(exc).__name__}: {exc}"
        if result.returncode or not output.is_file() or output.stat().st_size == 0:
            detail = result.stderr.decode(errors="replace").strip()[-1000:]
            return detail or f"hardware encoder exited {result.returncode}"
        try:
            import av

            with av.open(str(output)) as container:
                decoded = next(container.decode(video=0), None)
            if decoded is None:
                return "hardware probe MP4 contains no decodable frame"
        except Exception as exc:  # noqa: BLE001
            return f"hardware probe MP4 cannot be decoded: {exc}"
    return None


def select_backend(image: np.ndarray, fps: int) -> str:
    setting = os.environ.get(BACKEND_ENV, "auto").strip().lower()
    if setting not in {"auto", "libx264", "h264_rkmpp"}:
        raise ValueError(f"{BACKEND_ENV} must be auto, libx264, or h264_rkmpp")
    if setting == "libx264":
        return setting
    failure = rkmpp_probe(image, fps)
    if failure is None:
        return "h264_rkmpp"
    if setting == "h264_rkmpp":
        raise RuntimeError(f"forced h264_rkmpp probe failed: {failure}")
    return "libx264"


class PyAvVideo:
    def __init__(self, path: Path, width: int, height: int, fps: int):
        import av

        self.av = av
        self.width, self.height = width, height
        self.container = av.open(
            str(path),
            "w",
            options={"movflags": "frag_keyframe+empty_moov+default_base_moof"},
        )
        self.stream = self.container.add_stream(
            "libx264",
            rate=fps,
            options={"preset": "ultrafast", "crf": "20", "tune": "zerolatency"},
        )
        self.stream.width, self.stream.height = width, height
        self.stream.pix_fmt, self.stream.thread_count, self.stream.gop_size = (
            "yuv420p",
            1,
            fps,
        )

    def append(self, image: np.ndarray) -> None:
        frame = self.av.VideoFrame.from_ndarray(image, format="rgb24")
        for packet in self.stream.encode(frame):
            self.container.mux(packet)

    def close(self) -> None:
        try:
            for packet in self.stream.encode():
                self.container.mux(packet)
        finally:
            self.container.close()


class RkmppVideo:
    def __init__(self, path: Path, width: int, height: int, fps: int):
        self.width, self.height = width, height
        self.stderr = tempfile.TemporaryFile()
        self.process = subprocess.Popen(
            _command(RKMPP_FFMPEG, path, width, height, fps),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=self.stderr,
            bufsize=0,
        )
        self.closed = False

    def _failure(self) -> str:
        self.stderr.seek(0)
        detail = self.stderr.read().decode(errors="replace").strip()[-2000:]
        return detail or f"h264_rkmpp exited {self.process.returncode}"

    def append(self, image: np.ndarray) -> None:
        if self.process.stdin is None:
            raise RuntimeError("h264_rkmpp input is closed")
        try:
            remaining = _rgb_bytes(image)
            while remaining:
                written = self.process.stdin.write(remaining)
                if not written:
                    raise BrokenPipeError("h264_rkmpp accepted zero input bytes")
                remaining = remaining[written:]
        except BrokenPipeError as exc:
            self.process.wait(timeout=2)
            raise RuntimeError(self._failure()) from exc

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.process.stdin is not None:
                try:
                    self.process.stdin.close()
                except BrokenPipeError:
                    pass
            try:
                returncode = self.process.wait(timeout=15)
            except subprocess.TimeoutExpired as exc:
                self.process.terminate()
                self.process.wait(timeout=2)
                raise RuntimeError("h264_rkmpp did not finish within 15 seconds") from exc
            if returncode:
                raise RuntimeError(self._failure())
        finally:
            self.stderr.close()


def open_video(backend: str, path: Path, width: int, height: int, fps: int):
    if backend == "h264_rkmpp":
        return RkmppVideo(path, width, height, fps)
    return PyAvVideo(path, width, height, fps)
