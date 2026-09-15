import os
import threading

import pytest

from yam_abc_reproduce.resource_qos import place_on_cpus


def test_cpu_placement_is_opt_in(monkeypatch):
    monkeypatch.delenv("YAM_ABC_CONTROL_CPUS", raising=False)
    before = os.sched_getaffinity(0)
    assert place_on_cpus("CONTROL") is None
    assert os.sched_getaffinity(0) == before


def test_configured_cpu_placement_only_changes_its_worker_thread(monkeypatch):
    if not hasattr(os, "sched_setaffinity"):
        pytest.skip("Linux affinity required")
    before = os.sched_getaffinity(0)
    target = max(before)
    monkeypatch.setenv("YAM_ABC_ENCODER_CPUS", str(target))
    result = []
    thread = threading.Thread(target=lambda: result.append(place_on_cpus("ENCODER")))
    thread.start()
    thread.join(1)
    assert result == [(target,)]
    assert os.sched_getaffinity(0) == before


@pytest.mark.parametrize("setting", ["", "4-5", "0,x", "-1", "9999"])
def test_invalid_cpu_configuration_is_explicit(monkeypatch, setting):
    monkeypatch.setenv("YAM_ABC_PREVIEW_CPUS", setting)
    with pytest.raises(ValueError, match="invalid YAM_ABC_PREVIEW_CPUS"):
        place_on_cpus("PREVIEW")


def test_camera_worker_reports_invalid_cpu_placement_without_waiting_for_warmup(monkeypatch):
    from yam_abc_reproduce.camera.mock_camera import MockCamera
    from yam_abc_reproduce.camera.worker import CameraWorker

    monkeypatch.setenv("YAM_ABC_CAMERA_CPUS", "9999")
    worker = CameraWorker(MockCamera("top", "top", width=32, height=32))
    with pytest.raises(RuntimeError, match="CPU placement failed"):
        worker.start(warmup_timeout=2)
