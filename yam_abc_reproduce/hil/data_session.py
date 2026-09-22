"""Data-session lifecycle transactions, independent of SDK/CAN ownership.

Only the control tick grants a paused barrier and installs a replacement writer.
Directory creation, process startup and draining happen on the caller's worker.
Task binding and recovery share the barrier; completion never starts motion.
"""

import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class DataChange:
    committed: bool = False


@contextmanager
def paused_data_session(runtime, *, recovering=False):
    receipt = {"done": threading.Event(), "recovering": recovering}
    runtime.policy_commands.put_nowait(("task_barrier", receipt))
    if not receipt["done"].wait(2):
        receipt["cancelled"] = True
        runtime.task_releases.put(("task_release", False))
        raise ValueError("控制线程未确认数据会话切换，请检查设备状态")
    if receipt.get("error"):
        raise ValueError(receipt["error"])
    change = DataChange()
    try:
        yield change
    finally:
        released = threading.Event()
        runtime.task_releases.put(("task_release", change.committed, released))
        if not released.wait(2):
            raise ValueError("数据会话已处理，但控制线程未确认结束切换；请查看状态，不要重复操作")


@dataclass
class RecorderReplacement:
    previous: object
    replacement: object
    done: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    cancelled: bool = False
    accepted: bool = False
    error: str | None = None

    def apply(self, runtime):
        """Control owner only: no disk/process operations under this lock."""
        with self.lock:
            if (self.cancelled or not runtime.task_switching
                    or runtime.recorder is not self.previous
                    or runtime.session.arbiter.phase.value != "hold"):
                self.error = "设备状态已变化，录制恢复未应用"
            else:
                runtime.recorder = self.replacement
                runtime.recording_error = None
                runtime.outcome = "unknown"
                runtime._record_started = None
                self.accepted = True
            self.done.set()

    def install(self, runtime):
        runtime.task_releases.put(("recorder_replace", self))
        self.done.wait(2)
        with self.lock:
            if not self.accepted:
                self.cancelled = True  # A late tick cannot install a closed writer.
                raise RuntimeError(self.error or "控制线程未确认录制恢复；机械臂未被重连")


def recover_recording(runtime, change):
    """Replace a failed writer in a new directory, retaining all source files."""
    previous = runtime.recorder
    reason = runtime.recording_error or previous.error
    if not reason or previous.recording:
        raise ValueError("仅可恢复已暂停的故障录制")
    metadata = dict(previous.metadata)
    for key in ("terminal_status", "omitted_intervention_waits", "close_errors", "recording_error"):
        metadata.pop(key, None)
    metadata["recording_recovery"] = {"previous_session": str(previous.path), "reason": str(reason)}
    output = previous.path.parent / (
        time.strftime("session_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
    )
    candidate = type(previous)(
        output, mode=runtime.session.arbiter.mode.value, fps=previous.fps,
        metadata=metadata, segment_seconds=previous.segment_seconds,
        min_free_bytes=previous.min_free_bytes, video_backend=previous.video_backend,
        capacity=previous.queue.maxsize,
    )
    exchange = RecorderReplacement(previous, candidate)
    try:
        if candidate.error or not candidate._thread.is_alive():
            raise RuntimeError(candidate.error or "新录制线程启动失败")
        try:
            previous.close("aborted")
        except Exception as exc:
            # A dead remote owner may have no final acknowledgement. Preserve
            # its spool; do not claim that every queued frame was saved.
            candidate.metadata["recording_recovery"]["previous_close_error"] = str(exc)
        previous._thread.join(2)
        if previous._thread.is_alive():
            raise RuntimeError("旧录制线程尚未退出，未替换；请先检查存储或录制进程")
        if candidate.error or not candidate._thread.is_alive():
            raise RuntimeError(candidate.error or "新录制线程已退出")
        exchange.install(runtime)
        change.committed = True
        return output
    finally:
        if not exchange.accepted:
            candidate.close("aborted")
