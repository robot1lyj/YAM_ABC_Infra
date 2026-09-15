import json
import time
from types import SimpleNamespace

import pytest

from yam_abc_reproduce.hil.tasks import Tasks
from yam_abc_reproduce.hil.workbench import Workbench


def test_task_catalog_identity_persistence_and_validation(tmp_path):
    catalog = Tasks(tmp_path)
    task = catalog.create("乐高分拣", "按颜色将乐高放入对应盒子", "Sort the LEGO bricks by color.")
    assert Tasks(tmp_path).get(task["id"]) == task
    with pytest.raises(ValueError):
        catalog.create(" 乐高分拣 ", "另一条指令", "Sort the LEGO bricks by color.")
    with pytest.raises(ValueError):
        catalog.create("", "目标", "Sort the LEGO bricks by color.")
    with pytest.raises(ValueError):
        catalog.get("../../escape")
    catalog.path.write_text('[{"id":"../../escape"}]')
    broken = Tasks(tmp_path)
    assert broken.error
    with pytest.raises(ValueError):
        broken.create("任务", "目标", "Sort the LEGO bricks by color.")
    assert catalog.path.read_text() == '[{"id":"../../escape"}]'


def test_independent_connections_and_task_scoped_recording(tmp_path):
    service = Workbench(
        SimpleNamespace(
            mode="collect",
            mock=True,
            url=None,
            station="configs/station_hil.yaml",
            output=tmp_path / "sessions",
            task_root=tmp_path / "tasks",
            baseline=False,
            raw_only=True,
        )
    )
    service.preview_enabled = False

    def wait(predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            service.heartbeat()
            time.sleep(0.05)
        assert predicate(), service.status

    try:
        task = service.create_task("乐高分拣", "按颜色分拣乐高", "Sort the LEGO bricks by color.")
        service.connect()
        wait(lambda: service.runtime is not None and service.status.get("tick", 0) > 2)
        assert service.camera_state == "disconnected"
        assert all(c.worker is None for c in service.camera_slots)
        with pytest.raises(ValueError, match="相机"):
            service.event("start")
        with pytest.raises(ValueError):
            service.create_task("其他任务", "其他目标", "Sort the LEGO bricks by color.")
        service.connect_cameras()
        wait(lambda: service.camera_state == "connected")
        service.event("start")
        wait(lambda: service.status.get("phase") == "human")
        with pytest.raises(ValueError):
            service.disconnect_cameras()
        service.event("record")
        wait(lambda: service.status.get("recorded_steps", 0) > 5)
        service.event("record")
        wait(lambda: not service.status.get("recording"))
        output = service.output
        service.disconnect(supported=True)
        wait(lambda: not service.thread.is_alive(), 30)
        assert not service.error
        assert service.camera_state == "connected"
        assert all(c.read() is not None for c in service.camera_slots)
        session = json.loads((output / "session.json").read_text())
        assert session["task"] == task["task"]
        assert session["collection_task"] == task
        assert output.parent.name == task["id"]
        manifest = json.loads(
            (output / session["episodes"][0]["path"] / "manifest.json").read_text()
        )
        assert manifest["task"] == task["task"]
        assert manifest["collection_task"] == task
        assert manifest["station"]["task_name"] == task["task"]
        import pandas as pd

        from yam_abc_reproduce.hil.lerobot_export import export_session

        exported = output / "lerobot"
        report = export_session(output, exported)
        assert report["task"] == task["task"]
        assert report["collection_task"]["name"] == "乐高分拣"
        assert pd.read_parquet(exported / "meta/tasks.parquet").index[0] == task["task"]
        service.create_task("其他任务", "其他目标", "Sort the LEGO bricks by color.")
        service.disconnect_cameras()
        wait(lambda: service.camera_state == "disconnected")
    finally:
        service.close()


def test_taskless_teleop_can_run_but_never_record(tmp_path):
    service = Workbench(
        SimpleNamespace(
            mode="hil",
            mock=True,
            url=None,
            station="configs/station_hil.yaml",
            output=tmp_path / "sessions",
            task_root=tmp_path / "tasks",
            baseline=False,
            raw_only=True,
        )
    )
    service.preview_enabled = False

    def wait(predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            service.heartbeat()
            time.sleep(0.05)
        assert predicate(), service.status

    try:
        service.connect()
        wait(lambda: service.runtime is not None and service.status.get("tick", 0) > 2)
        assert service.taskless_teleop
        assert service.status["mode"] == "teleop"
        assert service.output.parent.name == "standalone-teleop"
        assert "teleop_sessions" in service.output.parts
        assert service.camera_state == "disconnected"

        service.event("start")
        wait(lambda: service.status.get("phase") == "human")
        with pytest.raises(ValueError, match="不提供录制"):
            service.event("record")
        with pytest.raises(ValueError, match="does not record"):
            service.runtime.event("record")
        assert not service.status.get("recording")

        output = service.output
        service.disconnect(supported=True)
        wait(lambda: not service.thread.is_alive(), 30)
        session = json.loads((output / "session.json").read_text())
        assert session["episodes"] == []
        assert session["collection_task"]["id"] == "standalone-teleop"
    finally:
        service.close()


def test_taskless_teleop_can_bind_collection_task_without_reconnecting_arms(tmp_path):
    task = Tasks(tmp_path / "tasks").create(
        "双臂采集", "操作后开始录制", "Record a bimanual demonstration."
    )
    service = Workbench(
        SimpleNamespace(
            mode="teleop",
            mock=True,
            url=None,
            station="configs/station_hil.yaml",
            output=tmp_path / "sessions",
            task_root=tmp_path / "tasks",
            baseline=False,
            raw_only=True,
        )
    )
    service.preview_enabled = False

    def wait(predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            service.heartbeat()
            time.sleep(0.05)
        assert predicate(), service.status

    try:
        service.connect()
        wait(lambda: service.runtime is not None and service.status.get("tick", 0) > 2)
        runtime, owner, standalone_output = service.runtime, service.thread, service.output
        assert service.taskless_teleop and not runtime.recording_allowed
        assert standalone_output.parents[3] == tmp_path
        service.event("start")
        wait(lambda: service.status.get("phase") == "human")
        with pytest.raises(ValueError, match="暂停遥操作"):
            service.select_task(task["id"])
        service.event("hold")
        wait(lambda: service.status.get("phase") == "hold")
        service.connect_cameras()
        wait(lambda: service.camera_state == "connected")
        assert service.select_task(task["id"]) == task
        assert service.runtime is runtime and service.thread is owner
        assert not service.taskless_teleop and runtime.recording_allowed
        assert service.output.parent.name == task["id"]
        assert not standalone_output.exists()
        with pytest.raises(ValueError, match="已绑定"):
            service.create_task("另一个任务", "不能混写", "Record another task.")
        service.event("mode:collect")
        wait(lambda: service.status.get("mode") == "collect")
        assert service.status["phase"] == "hold"
        service.event("start")
        wait(lambda: service.status.get("phase") == "human")
        service.event("record")
        wait(lambda: service.status.get("recorded_steps", 0) > 5)
        service.event("record")
        wait(lambda: not service.status.get("recording"))
        output = service.output
        service.disconnect(supported=True)
        wait(lambda: not service.thread.is_alive(), 30)
        assert service.error is None
        session = json.loads((output / "session.json").read_text())
        assert session["collection_task"] == task and session["task"] == task["task"]
        manifest = json.loads((output / session["episodes"][0]["path"] / "manifest.json").read_text())
        assert manifest["collection_task"] == task
        assert manifest["station"]["task_name"] == task["task"]
        assert manifest["steps"] > 5
    finally:
        if service.runtime is not None:
            service.disconnect(supported=True)
            if service.thread:
                service.thread.join(10)
        service.close()


def test_teleop_mode_never_records_even_with_a_selected_task(tmp_path):
    service = Workbench(
        SimpleNamespace(
            mode="teleop",
            mock=True,
            url=None,
            station="configs/station_hil.yaml",
            output=tmp_path / "sessions",
            task_root=tmp_path / "tasks",
            baseline=False,
            raw_only=True,
        )
    )
    service.preview_enabled = False

    def wait(predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            service.heartbeat()
            time.sleep(0.05)
        assert predicate(), service.status

    try:
        service.create_task("仅遥操作", "不采集数据", "Validate teleoperation only.")
        service.connect()
        wait(lambda: service.runtime is not None and service.status.get("tick", 0) > 2)
        service.event("start")
        wait(lambda: service.status.get("phase") == "human")
        time.sleep(0.15)
        with pytest.raises(ValueError, match="不提供录制"):
            service.event("record")
        assert not service.status.get("recording")
        assert service.runtime.recorder.episodes == []
    finally:
        if service.runtime is not None:
            service.disconnect(supported=True)
            if service.thread:
                service.thread.join(10)
        service.close()


@pytest.mark.parametrize("content", ["[1]", '[{"id":1}]', "null"])
def test_invalid_catalog_fails_closed(tmp_path, content):
    (tmp_path / "tasks.json").write_text(content)
    assert Tasks(tmp_path).error


def test_legacy_task_can_be_completed_without_changing_identity(tmp_path):
    catalog = Tasks(tmp_path)
    original = catalog.create("乐高分拣", "中文说明", "Sort LEGO.")
    legacy = {k: v for k, v in original.items() if k != "task"}
    catalog.path.write_text(json.dumps([legacy]))
    loaded = Tasks(tmp_path)
    assert loaded.error is None
    assert "task" not in loaded.get(original["id"])
    updated = loaded.update(original["id"], "乐高分拣", "中文说明", "Sort LEGO by color.")
    assert updated["id"] == original["id"]
    assert updated["created_at"] == original["created_at"]
    assert Tasks(tmp_path).get(original["id"])["task"] == "Sort LEGO by color."


@pytest.mark.parametrize("task", ["", "乐高分拣", "123", "Sort\nLEGO"])
def test_english_task_rejects_missing_or_non_english(tmp_path, task):
    with pytest.raises(ValueError, match="英文 task"):
        Tasks(tmp_path).create("乐高分拣", "中文说明", task)
