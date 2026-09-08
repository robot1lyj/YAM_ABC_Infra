import json
import time
from types import SimpleNamespace

import pytest

from yam_abc_reproduce.hil.tasks import Tasks
from yam_abc_reproduce.hil.workbench import Workbench


def test_task_catalog_identity_persistence_and_validation(tmp_path):
    catalog = Tasks(tmp_path)
    task = catalog.create("乐高分拣", "按颜色将乐高放入对应盒子")
    assert Tasks(tmp_path).get(task["id"]) == task
    with pytest.raises(ValueError):
        catalog.create(" 乐高分拣 ", "另一条指令")
    with pytest.raises(ValueError):
        catalog.create("", "目标")
    with pytest.raises(ValueError):
        catalog.get("../../escape")
    catalog.path.write_text('[{"id":"../../escape"}]')
    broken = Tasks(tmp_path)
    assert broken.error
    with pytest.raises(ValueError):
        broken.create("任务", "目标")
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
        with pytest.raises(ValueError, match="任务"):
            service.connect()
        task = service.create_task("乐高分拣", "按颜色分拣乐高")
        service.connect()
        wait(lambda: service.runtime is not None and service.status.get("tick", 0) > 2)
        assert service.camera_state == "disconnected"
        assert all(c.worker is None for c in service.camera_slots)
        with pytest.raises(ValueError, match="相机"):
            service.event("start")
        with pytest.raises(ValueError):
            service.create_task("其他任务", "其他目标")
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
        assert session["task"] == task
        assert output.parent.name == task["id"]
        manifest = json.loads(
            (output / session["episodes"][0]["path"] / "manifest.json").read_text()
        )
        assert manifest["task"] == task
        assert manifest["station"]["task_name"] == task["instruction"]
        service.create_task("其他任务", "其他目标")
        service.disconnect_cameras()
        wait(lambda: service.camera_state == "disconnected")
    finally:
        service.close()


@pytest.mark.parametrize("content", ["[1]", '[{"id":1}]', "null"])
def test_invalid_catalog_fails_closed(tmp_path, content):
    (tmp_path / "tasks.json").write_text(content)
    assert Tasks(tmp_path).error
