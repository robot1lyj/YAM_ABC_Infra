"""Stopping a long episode may drain longer than the old fixed close deadlines."""

import pytest

from yam_abc_reproduce.hil import recording, recording_process


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Value:
    def __init__(self, value=0.0):
        self.value = value


class WriterThread:
    def __init__(self, clock, progress, *, advancing):
        self.clock = clock
        self.progress = progress
        self.advancing = advancing
        self.joins = 0

    def is_alive(self):
        return self.joins < 5

    def join(self, _timeout):
        self.clock.now += 40
        self.joins += 1
        if self.advancing:
            self.progress.value = self.clock.now


def test_save_wait_allows_more_than_35_seconds_while_progressing(monkeypatch):
    clock = Clock()
    progress = Value()
    monkeypatch.setattr(recording.time, "monotonic", clock)
    thread = WriterThread(clock, progress, advancing=True)
    recording.wait_for_save(thread, lambda: progress.value, "test writer")
    assert clock.now == 200


def test_save_wait_reports_stalled_writer(monkeypatch):
    clock = Clock()
    progress = Value()
    monkeypatch.setattr(recording.time, "monotonic", clock)
    thread = WriterThread(clock, progress, advancing=False)
    with pytest.raises(RuntimeError, match="no save progress"):
        recording.wait_for_save(thread, lambda: progress.value, "test writer")
    assert clock.now == 160


class Process:
    def __init__(self, clock, progress):
        self.clock = clock
        self.progress = progress
        self.joins = 0
        self.alive = True

    def is_alive(self):
        return self.alive and self.joins < 5

    def join(self, _timeout):
        self.clock.now += 40
        self.joins += 1
        self.progress.value = self.clock.now

    def terminate(self):
        self.alive = False


class Queue:
    def __init__(self, result=None):
        self.result = result
        self.closed = False

    def put(self, _item, timeout=None):
        assert timeout is not None

    def get(self, timeout=None):
        assert timeout is not None
        return self.result

    def cancel_join_thread(self):
        pass

    def close(self):
        self.closed = True


def test_encoder_close_waits_for_long_progressing_drain(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(recording_process.time, "monotonic", clock)
    encoder = recording_process.EncoderProcess.__new__(recording_process.EncoderProcess)
    encoder.last_progress_at = Value()
    encoder.process = Process(clock, encoder.last_progress_at)
    encoder.incoming = Queue()
    encoder.free = Queue()
    encoder.result = Queue({"error": None, "segments": 2})
    assert encoder.close("success", {}) == {"error": None, "segments": 2}
    assert clock.now == 200
    assert encoder.incoming.closed and encoder.free.closed and encoder.result.closed
