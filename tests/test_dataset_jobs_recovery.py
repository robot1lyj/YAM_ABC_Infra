"""Real subprocess loss and durable admission failures for offline jobs."""

import json
import os
import time
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pytest

from yam_abc_reproduce.dataset_workbench import jobs as job_module
from yam_abc_reproduce.dataset_workbench.catalog import Catalog
from yam_abc_reproduce.dataset_workbench.jobs import Jobs


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("offline job did not reach the expected state")


def crash_when_released(root, identity, kind, request, payload):
    """Spawn-importable stand-in for a native worker crash after partial work."""
    catalog = Catalog(root, recover_jobs=False)
    payload.update(progress=7, message="partial work")
    with catalog.db() as db:
        db.execute(
            "UPDATE jobs SET state='running',payload=? WHERE id=?", (json.dumps(payload), identity)
        )
    folder = Path(request["path"])
    (folder / "started").touch()
    wait_for(lambda: (folder / "release").exists())
    os._exit(23)


def complete_then_crash(root, identity, kind, request, payload):
    catalog = Catalog(root, recover_jobs=False)
    payload.update(message="已完成", report_available=True)
    with catalog.db() as db:
        db.execute(
            "UPDATE jobs SET state='complete',payload=? WHERE id=?", (json.dumps(payload), identity)
        )
    os._exit(24)


@pytest.fixture
def jobs(tmp_path):
    service = Jobs(Catalog(tmp_path / "catalog"))
    try:
        yield service
    finally:
        service.close()


def complete_import(jobs, path):
    identity = jobs.submit("import", {"path": str(path)})
    wait_for(lambda: any(j["id"] == identity and j["state"] == "complete" for j in jobs.list()))
    wait_for(lambda: jobs.slots._value == 4)
    return identity


def test_catalog_write_failure_releases_all_admission_slots(jobs, tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("database disk full")

    with monkeypatch.context() as fault:
        fault.setattr(jobs, "save", fail)
        for _ in range(6):
            with pytest.raises(ValueError, match="后台任务未提交.*disk full"):
                jobs.submit("import", {"path": str(tmp_path)})
    assert jobs.slots._value == 4
    assert jobs.list() == []
    complete_import(jobs, tmp_path)


def test_synchronous_submit_failure_is_terminal_and_capacity_recovers(jobs, tmp_path, monkeypatch):
    def fail(*args):
        raise RuntimeError("worker could not start")

    with monkeypatch.context() as fault:
        fault.setattr(jobs.pool, "submit", fail)
        for _ in range(6):
            with pytest.raises(ValueError, match="后台任务未提交.*could not start"):
                jobs.submit("import", {"path": str(tmp_path)})
    assert jobs.slots._value == 4
    assert len(jobs.list()) == 6
    assert all(job["state"] == "failed" for job in jobs.list())
    complete_import(jobs, tmp_path)


def test_worker_crash_finishes_entire_queue_and_explicit_submit_uses_new_pool(
    jobs,
    tmp_path,
    monkeypatch,
):
    old_pool = jobs.pool
    with monkeypatch.context() as fault:
        fault.setattr(job_module, "execute_job", crash_when_released)
        first = jobs.submit("import", {"path": str(tmp_path)})
        wait_for(lambda: (tmp_path / "started").exists())
        for _ in range(3):
            jobs.submit("import", {"path": str(tmp_path)})
        with pytest.raises(ValueError, match="已有4项任务"):
            jobs.submit("import", {"path": str(tmp_path)})
        # A concurrent API submission may hold this lock when the worker dies.
        # Completion callbacks must still finish without a lock-order inversion.
        with jobs._lock:
            (tmp_path / "release").touch()
            wait_for(lambda: jobs.slots._value == 4)
    failed = jobs.list()
    assert len(failed) == 4 and all(job["state"] == "failed" for job in failed)
    assert next(job for job in failed if job["id"] == first)["progress"] == 7
    assert all("未自动重跑" in job["message"] for job in failed)
    assert jobs.pool is old_pool and jobs._pool_failed.is_set()
    complete_import(jobs, tmp_path)
    assert jobs.pool is not old_pool
    assert sum(job["state"] == "failed" for job in jobs.list()) == 4


def test_already_broken_pool_rejects_once_without_leaking_or_replaying(jobs, tmp_path):
    with pytest.raises(BrokenProcessPool):
        jobs.pool.submit(os._exit, 25).result(timeout=10)
    with pytest.raises(ValueError, match="后台任务未提交"):
        jobs.submit("import", {"path": str(tmp_path)})
    assert jobs.slots._value == 4
    assert jobs._pool_failed.is_set()
    assert jobs.list()[0]["state"] == "failed"
    complete_import(jobs, tmp_path)


def test_worker_exit_does_not_overwrite_durable_completion(jobs, tmp_path, monkeypatch):
    monkeypatch.setattr(job_module, "execute_job", complete_then_crash)
    identity = jobs.submit("import", {"path": str(tmp_path)})
    wait_for(lambda: jobs.slots._value == 4)
    assert jobs.list()[0]["id"] == identity
    assert jobs.list()[0]["state"] == "complete"
    assert jobs.list()[0]["report_available"]


def test_service_restart_marks_only_unfinished_jobs_interrupted(tmp_path):
    catalog = Catalog(tmp_path / "catalog")
    jobs = Jobs(catalog)
    for state in ("queued", "running", "complete", "failed"):
        jobs.save(state, state, {"kind": "导入索引", "progress": 3, "message": state})
    jobs.close()
    restarted = Jobs(Catalog(catalog.root))
    try:
        states = {job["id"]: job for job in restarted.list()}
        for identity in ("queued", "running"):
            assert states[identity]["state"] == "interrupted"
            assert states[identity]["progress"] == 3
            assert "未自动重跑" in states[identity]["message"]
        assert states["complete"]["state"] == "complete"
        assert states["failed"]["state"] == "failed"
        assert restarted.slots._value == 4
        complete_import(restarted, tmp_path)
    finally:
        restarted.close()
