"""Bounded startup/cleanup failures; no robot, CAN, or physical camera access."""

import builtins
import threading
from types import SimpleNamespace

import pytest

from yam_abc_reproduce.camera.worker import CameraWorker
from yam_abc_reproduce.hil.workbench import Workbench


def service(tmp_path):
    owner = Workbench.__new__(Workbench)
    owner.args = SimpleNamespace(
        mock=True, station="unused.yaml", url=None, baseline=False, output=str(tmp_path)
    )
    owner.mode = "collect"
    owner._lock = threading.Lock()
    owner._closing = threading.Event()
    owner.runtime = None
    owner.thread = owner.camera_thread = None
    owner.state = owner.camera_state = "disconnected"
    owner.error = owner.camera_error = owner.cleanup_error = None
    owner.video_backend = None
    owner.selected_task = None
    owner._camera_generation = 0
    owner._camera_workers = []
    owner.camera_slots = [
        SimpleNamespace(role=role, worker=None) for role in ("top", "left", "right")
    ]
    owner.log = lambda message: None
    return owner


class Driver:
    def __init__(self, role="top", stop_failures=0):
        self.role = self.name = self.serial = role
        self.mode = "mono"
        self.stop_failures = stop_failures
        self.stop_calls = 0

    def stop(self):
        self.stop_calls += 1
        if self.stop_calls <= self.stop_failures:
            raise RuntimeError(f"{self.role} close failed")


@pytest.mark.parametrize("operation", ["camera_connect", "camera_disconnect", "connect"])
@pytest.mark.parametrize("boundary", ["constructor", "start"])
def test_owner_thread_failure_is_terminal_and_explicit_retry_works(
    monkeypatch, tmp_path, operation, boundary
):
    owner = service(tmp_path)
    driver = Driver()
    if operation == "camera_connect":
        attempt = owner.connect_cameras
        target = "_open_cameras"
    elif operation == "camera_disconnect":
        owner.camera_state = "connected"
        owner._camera_workers = [driver]
        attempt = owner.disconnect_cameras
        target = "_close_cameras"
    else:
        def attempt():
            owner.connect(ready=True, initialize=True)

        target = "_run"
    called = threading.Event()
    monkeypatch.setattr(owner, target, called.set)

    def unavailable(*args, **kwargs):
        raise RuntimeError("cannot allocate worker thread")

    with monkeypatch.context() as failure:
        if boundary == "constructor":
            failure.setattr(threading, "Thread", unavailable)
        else:
            failure.setattr(threading.Thread, "start", unavailable)
        with pytest.raises(RuntimeError, match="cannot allocate"):
            attempt()
    assert not called.is_set()  # No implicit retry or motion after failed admission.
    if operation == "connect":
        assert owner.state == "fault" and owner.thread is None
        assert not owner.initializing and not owner.taskless_teleop
    else:
        assert owner.camera_state == "fault" and owner.camera_thread is None
    if operation == "camera_disconnect":
        assert owner._camera_workers == [driver] and driver.stop_calls == 0
    attempt()
    thread = owner.thread if operation == "connect" else owner.camera_thread
    thread.join(2)
    assert not thread.is_alive() and called.is_set()


def test_partial_camera_startup_retains_failed_workers_and_unwrapped_drivers(
    monkeypatch, tmp_path
):
    from yam_abc_reproduce import config, runtime
    from yam_abc_reproduce.camera import worker

    owner = service(tmp_path)
    drivers = [Driver("top", 1), Driver("left", 1), Driver("right")]
    wrapped = []
    factory_calls = []

    class Worker:
        def __init__(self, driver):
            if driver.role == "left":
                raise RuntimeError("worker construction failed")
            self.driver, self.role = driver, driver.role
            wrapped.append(self)

        def start(self):
            pass

        def stop(self):
            self.driver.stop()

    monkeypatch.setattr(config, "build_station_config", lambda _: SimpleNamespace(cameras=drivers))
    monkeypatch.setattr(
        runtime, "build_cameras_from_config", lambda *a, **kw: factory_calls.append(1) or drivers
    )
    monkeypatch.setattr(worker, "CameraWorker", Worker)
    owner.connect_cameras()
    owner.camera_thread.join(2)
    assert owner.camera_state == "fault"
    assert "worker construction failed" in owner.camera_error
    assert "top close failed" in owner.camera_error and "left close failed" in owner.camera_error
    assert owner._camera_workers == [wrapped[0], drivers[1]]
    assert all(slot.worker is None for slot in owner.camera_slots)
    assert [d.stop_calls for d in drivers] == [1, 1, 1]
    with pytest.raises(ValueError, match="重试断开"):
        owner.connect_cameras()
    assert len(factory_calls) == 1
    owner.disconnect_cameras()
    owner.camera_thread.join(2)
    assert owner.camera_state == "disconnected" and not owner._camera_workers
    assert [d.stop_calls for d in drivers] == [2, 2, 1]


def test_failed_close_preserves_only_pending_resources_for_retry(tmp_path):
    owner = service(tmp_path)
    closed, pending = Driver("top"), Driver("left", 1)
    owner._camera_workers = [closed, pending]
    owner.camera_state = "connected"
    owner.camera_slots[0].worker = closed
    owner.disconnect_cameras()
    owner.camera_thread.join(2)
    assert owner.camera_state == "fault" and owner._camera_workers == [pending]
    assert all(slot.worker is None for slot in owner.camera_slots)
    with pytest.raises(ValueError, match="重试断开"):
        owner.connect_cameras()
    owner.disconnect_cameras()
    owner.camera_thread.join(2)
    assert owner.camera_state == "disconnected" and owner.camera_error is None
    assert owner._camera_workers == []
    assert closed.stop_calls == 1 and pending.stop_calls == 2


@pytest.mark.parametrize("operation", ["connect_cameras", "disconnect_cameras"])
def test_camera_owner_cannot_be_replaced_while_cleanup_still_running(tmp_path, operation):
    owner = service(tmp_path)
    owner.camera_state = "fault"
    owner.camera_thread = SimpleNamespace(is_alive=lambda: True)
    with pytest.raises(ValueError, match="切换状态"):
        getattr(owner, operation)()


@pytest.mark.parametrize("boundary", ["import", "argv"])
def test_session_setup_failure_finishes_owner_and_allows_explicit_retry(
    monkeypatch, tmp_path, boundary
):
    owner = service(tmp_path)
    original_import = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name == "resource_qos":
            raise ImportError("control startup import failed")
        return original_import(name, *args, **kwargs)

    class InvalidOption:
        def __str__(self):
            raise ValueError("control startup argv failed")

    with monkeypatch.context() as failure:
        if boundary == "import":
            failure.setattr(builtins, "__import__", unavailable)
        else:
            owner.args.segment_seconds = InvalidOption()
        owner.connect(ready=True, initialize=True)
        owner.thread.join(2)
    assert not owner.thread.is_alive()
    assert owner.state == "fault" and f"startup {boundary} failed" in owner.error
    assert not owner.initializing and not owner.taskless_teleop and owner.runtime is None
    retried = threading.Event()
    monkeypatch.setattr(owner, "_run_session", retried.set)
    assert not retried.is_set()
    owner.connect(ready=True, initialize=True)
    owner.thread.join(2)
    assert retried.is_set() and owner.state == "disconnected" and owner.error is None


def test_camera_worker_start_failure_still_closes_driver(monkeypatch):
    driver = Driver()
    worker = CameraWorker(driver)

    def unavailable(thread):
        raise RuntimeError("cannot start capture thread")

    monkeypatch.setattr(threading.Thread, "start", unavailable)
    with pytest.raises(RuntimeError, match="cannot start"):
        worker.start()
    worker.stop()
    assert worker._thread is None and driver.stop_calls == 1


def test_real_camera_worker_start_failure_releases_entire_partial_connection(monkeypatch, tmp_path):
    from yam_abc_reproduce import config, runtime

    owner = service(tmp_path)
    drivers = [Driver(role) for role in ("top", "left", "right")]
    original_start = threading.Thread.start

    def unavailable_capture(thread):
        if thread.name != "camera-owner":
            raise RuntimeError("cannot start capture thread")
        return original_start(thread)

    monkeypatch.setattr(config, "build_station_config", lambda _: SimpleNamespace(cameras=drivers))
    monkeypatch.setattr(runtime, "build_cameras_from_config", lambda *a, **kw: drivers)
    monkeypatch.setattr(threading.Thread, "start", unavailable_capture)
    owner.connect_cameras()
    owner.camera_thread.join(2)
    assert not owner.camera_thread.is_alive()
    assert owner.camera_state == "fault" and "cannot start capture" in owner.camera_error
    assert owner._camera_workers == [] and all(d.stop_calls == 1 for d in drivers)
    assert all(slot.worker is None for slot in owner.camera_slots)


def test_camera_worker_live_reader_keeps_handle_until_explicit_stop_retry():
    driver = Driver()
    worker = CameraWorker(driver)
    release = threading.Event()
    thread = threading.Thread(target=release.wait, daemon=True)
    worker._thread = thread
    worker._running = True
    thread.start()
    try:
        with pytest.raises(RuntimeError, match="reader has not stopped"):
            worker.stop(join_timeout=0)
        assert worker._thread is thread and thread.is_alive()
        assert driver.stop_calls == 0
    finally:
        release.set()
        thread.join(2)
    worker.stop(join_timeout=0)
    assert worker._thread is None and driver.stop_calls == 1
