"""Task counts/delete are storage operations, never robot-control operations."""

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from yam_abc_reproduce.hil.recording import RecordingSession
from yam_abc_reproduce.hil.recording_service import RemoteRecordingSession
from yam_abc_reproduce.hil.storage import atomic_json
from yam_abc_reproduce.hil.task_dataset import TaskDatasetCache, inventory, trash_episode
from yam_abc_reproduce.hil.web import create_app
from yam_abc_reproduce.hil.workbench import Workbench


def saved(root, session, number, steps=12, outcome="unknown"):
    path = root / session / f"episode_{number:06d}"
    path.mkdir(parents=True)
    atomic_json(path / "manifest.json", {"steps": steps})
    manifest = path.parent / "session.json"
    data = json.loads(manifest.read_text()) if manifest.exists() else {"episodes": []}
    data["episodes"].append({"path": path.name, "steps": steps, "outcome": outcome})
    atomic_json(manifest, data)
    return f"{session}/{path.name}"


def wait(predicate):
    deadline = time.monotonic() + 10
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.02)
    assert predicate()


def test_inventory_across_sessions_and_recoverable_delete(tmp_path):
    root = tmp_path / "task"
    first = saved(root, "session_a", 2, 3002)
    latest = saved(root, "session_b", 1, 30)
    saved(root, "session_b", 2, 5, "aborted")
    saved(root, "session_b", 3, 0)
    state = inventory(root, "task")
    assert state["count"] == 2 and state["frames"] == 3032
    assert state["latest"]["key"] == latest
    kept = (root / first / "manifest.json").read_bytes()
    result = trash_episode(root, "task", latest)
    assert not (root / latest).exists()
    assert (Path(result["trash"]) / "episode_000001/manifest.json").is_file()
    restore = json.loads((Path(result["trash"]) / "restore.json").read_text())
    assert len(restore["session_manifest"]["episodes"]) == 3
    assert inventory(root, "task")["count"] == 1
    assert (root / first / "manifest.json").read_bytes() == kept
    with pytest.raises(ValueError, match="已变化"):
        trash_episode(root, "task", latest)


def test_changed_latest_or_malicious_key_never_deletes(tmp_path):
    first = saved(tmp_path, "session_a", 1)
    latest = saved(tmp_path, "session_a", 2)
    for key in (first, "../../outside", "session_a/../episode_000002"):
        with pytest.raises(ValueError):
            trash_episode(tmp_path, "task", key)
    assert (tmp_path / first).is_dir() and (tmp_path / latest).is_dir()


def test_manifest_failure_rolls_back_data(tmp_path, monkeypatch):
    import yam_abc_reproduce.hil.task_dataset as module
    key = saved(tmp_path, "session_a", 1)
    before = (tmp_path / "session_a/session.json").read_bytes()
    original = module.atomic_json

    def fail(path, value):
        if path.name == "session.json":
            raise OSError("disk unavailable")
        original(path, value)

    monkeypatch.setattr(module, "atomic_json", fail)
    with pytest.raises(OSError):
        trash_episode(tmp_path, "task", key)
    assert (tmp_path / key).is_dir()
    assert (tmp_path / "session_a/session.json").read_bytes() == before


def test_post_replace_failure_restores_manifest_too(tmp_path, monkeypatch):
    import yam_abc_reproduce.hil.task_dataset as module
    key = saved(tmp_path, "session_a", 1)
    original = module.atomic_json
    failed = False

    def fail_after_replace(path, value):
        nonlocal failed
        original(path, value)
        if path.name == "session.json" and not failed:
            failed = True
            raise OSError("directory fsync failed")

    monkeypatch.setattr(module, "atomic_json", fail_after_replace)
    with pytest.raises(OSError):
        trash_episode(tmp_path, "task", key)
    assert inventory(tmp_path, "task")["count"] == 1


@pytest.mark.parametrize("session_type", [RecordingSession, RemoteRecordingSession])
def test_writer_delete_updates_ram_frames_and_does_not_reuse_ids(tmp_path, session_type):
    root = tmp_path / "task"
    recorder = session_type(root / "session_current", mode="collect")

    def record():
        recorder.start_episode()
        assert recorder.submit({"tick": 1}, {})
        with pytest.raises((ValueError, RuntimeError), match="结束录制"):
            recorder.delete_saved_episode(root, "task", "session_current/episode_000001")
        recorder.stop_episode()
        wait(lambda: not recorder.saving and bool(recorder.episodes))

    try:
        record()
        assert recorder.written == 1
        recorder.delete_saved_episode(root, "task", "session_current/episode_000001")
        assert recorder.episodes == [] and recorder.written == 0 and not recorder.error
        # A stale delete is an operation rejection, not a permanent writer fault.
        with pytest.raises(RuntimeError, match="已变化"):
            recorder.delete_saved_episode(root, "task", "session_current/episode_000001")
        assert not recorder.error
        record()
        assert recorder.episodes[0]["path"] == "episode_000002"
        assert recorder.written == 1
    finally:
        recorder.close()
    assert json.loads((recorder.path / "session.json").read_text())["episodes"] == recorder.episodes
    assert inventory(root, "task")["count"] == 1


def test_cache_never_blocks_status_or_republishes_predelete_scan(tmp_path):
    gate = threading.Event()
    cache = TaskDatasetCache(lambda _: (gate.wait(2), tmp_path)[1])
    start = time.monotonic()
    assert cache.snapshot("task")["loading"]
    assert time.monotonic() - start < .1
    cache.publish({"task_id": "task", "count": 9, "latest": None})
    gate.set()
    cache.reader.join(2)
    assert cache.snapshot("task")["count"] == 9
    assert cache.snapshot("other")["count"] is None


@pytest.mark.parametrize("session_type", [RecordingSession, RemoteRecordingSession])
def test_writer_can_delete_latest_from_a_previous_closed_session(tmp_path, session_type):
    root = tmp_path / "task"
    key = saved(root, "session_previous", 2, 3002)
    recorder = session_type(root / "session_current", mode="collect")
    try:
        recorder.delete_saved_episode(root, "task", key)
        assert recorder.written == 0 and recorder.episodes == [] and not recorder.error
        assert inventory(root, "task")["count"] == 0
    finally:
        recorder.close()


def test_connected_delete_uses_barrier_without_reconnecting_devices(tmp_path):
    service = Workbench(SimpleNamespace(mode="collect", mock=True, url=None,
        station="configs/station_hil.yaml", output=tmp_path / "episodes",
        task_root=tmp_path / "tasks", baseline=False, raw_only=True))
    service.preview_enabled = False

    def check(predicate):
        def alive():
            service.heartbeat()
            return predicate()
        wait(alive)

    try:
        task = service.create_task("测试", "目标", "Pick a brick.")
        service.connect_cameras()
        check(lambda: service.camera_state == "connected")
        service.connect()
        check(lambda: service.status.get("tick", 0) > 2)
        owner, runtime = service.thread, service.runtime
        service.event("start")
        check(lambda: service.status.get("phase") == "human")
        service.event("record")
        check(lambda: service.status.get("recorded_steps", 0) > 2)
        service.event("record")
        check(lambda: not runtime.recorder.recording and not runtime.recorder.saving
              and bool(runtime.recorder.episodes))
        service.event("hold")
        check(lambda: service.status.get("phase") == "hold" and not runtime.recorder.saving
              and bool(runtime.recorder.episodes))
        root = service.output.parent
        key = inventory(root, task["id"])["latest"]["key"]
        service.delete_last_episode(task_id=task["id"], expected_key=key)
        check(lambda: service.status.get("recorded_steps") == 0)
        assert service.runtime is runtime and service.thread is owner and owner.is_alive()
        assert service.camera_state == "connected" and service.state == "connected"
        assert service.status["task_dataset"]["count"] == 0
        assert service.status["episode_count"] == 0
        assert service.status["episodes"] == []
        service.event("start")
        check(lambda: service.status.get("phase") == "human")
        service.event("record")
        check(lambda: service.status.get("recorded_steps", 0) > 2)
        service.event("record")
        check(lambda: not runtime.recorder.recording and not runtime.recorder.saving
              and bool(runtime.recorder.episodes))
        service.event("hold")
        check(lambda: service.status.get("phase") == "hold" and not runtime.recorder.saving
              and bool(runtime.recorder.episodes))
        assert runtime.recorder.episodes[0]["path"] == "episode_000002"
        assert inventory(root, task["id"])["count"] == 1
    finally:
        service.close()


def test_disconnected_workbench_delete_and_task_guard(tmp_path):
    service = Workbench(SimpleNamespace(mode="collect", mock=True, url=None,
        output=tmp_path / "episodes", task_root=tmp_path / "tasks"))
    try:
        task = service.create_task("测试", "目标", "Pick a brick.")
        root = tmp_path / "episodes" / task["id"]
        key = saved(root, "session_a", 1)
        with TestClient(create_app(service)) as client:
            headers = {"X-YAM-Control": "1"}
            body = {"task_id": task["id"], "expected_key": key}
            assert client.post("/recording/delete-last", json=body).status_code == 403
            assert client.post("/recording/delete-last", json={**body, "expected_key": "../../bad"},
                               headers=headers).status_code == 422
            assert client.post("/recording/delete-last", json={**body, "task_id": "wrong"},
                               headers=headers).status_code == 409
            assert client.post("/recording/delete-last", json=body, headers=headers).status_code == 200
            assert client.get("/status").json()["task_dataset"]["count"] == 0
    finally:
        service.close()


@pytest.mark.parametrize("changes", [
    {"phase": "policy"}, {"phase": "human"}, {"intervention_pending": True},
    {"maintenance": "home"}, {"recording": True}, {"saving": True},
    {"task_switching": True}, {"recording_error": "disk error"},
])
def test_delete_rejects_active_states(tmp_path, changes):
    service = Workbench(SimpleNamespace(mode="collect", mock=True, url=None,
        output=tmp_path / "episodes", task_root=tmp_path / "tasks"))
    try:
        task = service.create_task("测试", "目标", "Pick a brick.")
        key = saved(tmp_path / "episodes" / task["id"], "session_a", 1)
        service.state = "connected"
        service.runtime = SimpleNamespace(
            status={"phase": changes.get("phase", "hold"),
                    "maintenance": changes.get("maintenance", "idle"),
                    "intervention_pending": changes.get("intervention_pending", False)},
            recorder=SimpleNamespace(recording=changes.get("recording", False),
                                     saving=changes.get("saving", False)),
            task_switching=changes.get("task_switching", False),
            recording_error=changes.get("recording_error"))
        with pytest.raises(ValueError, match="暂停"):
            service.delete_last_episode(task_id=task["id"], expected_key=key)
        assert (tmp_path / "episodes" / task["id"] / key).is_dir()
    finally:
        service.runtime = None
        service.close()


def test_browser_delete_uses_confirmation_and_original_target():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node required for UI event regression; run on development host")
    source = Path("yam_abc_reproduce/hil/static/app.js").read_text()
    funcs = source[source.index("function canDeleteEpisode"):source.index("function camerasConnected")]
    handler = source[source.index('$("delete-last-episode").onclick'):source.index('$("expand-vision").onclick')]
    code = """
    const assert=require('node:assert/strict');
    const button={}; const $=()=>button; const text=()=>{};
    let online=true,busy=false,deletingEpisode=false;
    let state={selected_task:{id:'task',name:'测试'},connection:'disconnected',
      task_dataset:{task_id:'task',latest:{key:'session_a/episode_000001',steps:12},count:1}};
    const activeDevice=()=>true; let confirm,requests=[];
    const confirmAction=(title,description,cb)=>{confirm=cb;assert(description.includes('12 帧'));};
    const toast=()=>{};
    const action=async(path,body)=>{requests.push({path,body});return true;};
    """ + funcs + handler + """
    (async()=>{
      assert(canDeleteEpisode()); button.onclick(); assert.equal(requests.length,0);
      state.task_dataset.latest.key='session_a/episode_000002'; await confirm();
      assert.equal(requests.length,0);
      button.onclick(); await confirm();
      assert.equal(requests.length,1);
      assert.equal(requests[0].body.expected_key,'session_a/episode_000002');
      for(const field of ['recording','recording_saving','intervention_pending','task_switching']){
        state[field]=true; assert(!canDeleteEpisode()); state[field]=false;
      }
      state.connection='connected';state.phase='policy';assert(!canDeleteEpisode());
      state.phase='hold';assert(canDeleteEpisode());online=false;assert(!canDeleteEpisode());
    })().catch(e=>{console.error(e);process.exit(1)});
    """
    subprocess.run([node, "-e", code], check=True, capture_output=True, text=True)
