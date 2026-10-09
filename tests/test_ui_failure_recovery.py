"""Recovery paths without robot, camera, CAN or model access."""

import builtins
import json
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from yam_abc_reproduce.hil.workbench import Workbench


def session_state(output, **changes):
    return dict(output=str(output), intervention_id=6, episodes=[], episode_count=0,
        recorded_steps=0, recording=False, recording_saving=False,
        intervention_pending=False, **changes)


def saved_first_row(output, row):
    episode = output / "episode_000001"
    episode.mkdir(parents=True)
    (episode / "manifest.json").write_text(json.dumps({"schema": "yam_hil_v1"}))
    (episode / "steps.jsonl").write_text(json.dumps(row) + "\n")


def test_status_counts_current_session_without_resetting_provenance(tmp_path):
    from fastapi.testclient import TestClient

    from yam_abc_reproduce.hil.web import create_app

    owner = SimpleNamespace(status=session_state(tmp_path / "first"))
    with TestClient(create_app(owner)) as client:
        state = client.get("/status").json()
        assert state["session_intervention_count"] == 0
        assert state["intervention_id"] == owner.status["intervention_id"] == 6
        owner.status.update(recording=True, intervention_id=7)
        assert client.get("/status").json()["session_intervention_count"] == 1
        owner.status = session_state(tmp_path / "second")
        owner.status["intervention_id"] = 7
        assert client.get("/status").json()["session_intervention_count"] == 0


@pytest.mark.parametrize("row,current,expected", [
    ({"intervention_id": 6, "is_intervention": False}, 6, 0),
    ({"intervention_id": 6, "is_intervention": False}, 7, 1),
    ({"intervention_id": 7, "is_intervention": True}, 7, 1),
    ({"intervention_id": 7, "transitions": ["takeover_applied"]}, 8, 2),
])
def test_web_restart_recovers_session_count_from_recording(tmp_path, row, current, expected):
    from yam_abc_reproduce.hil.web import _SessionInterventions

    saved_first_row(tmp_path, row)
    state = session_state(tmp_path)
    state.update(episodes=[{"path": "episode_000001"}], recorded_steps=2261,
        intervention_id=current)
    counter = _SessionInterventions()
    assert counter.count(state) is None
    counter.reader.join(2)
    assert counter.count(state) == expected
    reader = counter.reader
    assert counter.count(state) == expected
    assert counter.reader is reader  # Cached, not a disk scan on every poll.


def test_count_recovery_does_not_block_status_or_leak_across_tasks(tmp_path, monkeypatch):
    from yam_abc_reproduce.hil import web

    saved_first_row(tmp_path, {"intervention_id": 6})
    blocked, release = threading.Event(), threading.Event()

    def slow_rows(path):
        blocked.set()
        assert release.wait(2)
        yield {"intervention_id": 6}

    monkeypatch.setattr(web, "read_rows", slow_rows)
    counter = web._SessionInterventions()
    state = session_state(tmp_path)
    state.update(recording=True)
    assert counter.count(state) is None
    assert blocked.wait(1)
    try:
        assert counter.count(state) is None  # Does not wait for the disk reader.
        new_state = session_state(tmp_path / "new")
        new_state["intervention_id"] = 9
        assert counter.count(new_state) == 0
    finally:
        release.set()
        counter.reader.join(2)
    new_state.update(recording=True, intervention_id=10)
    assert counter.count(new_state) == 1


def test_unreadable_session_count_is_unknown_not_global_id(tmp_path):
    from yam_abc_reproduce.hil.web import _SessionInterventions

    saved_first_row(tmp_path, {"intervention_id": None})
    counter = _SessionInterventions()
    state = session_state(tmp_path)
    state.update(recorded_steps=2261)
    assert counter.count(state) is None
    counter.reader.join(2)
    assert counter.count(state) is None
    assert counter.count({}) is None


def camera_service():
    service = Workbench.__new__(Workbench)
    service.args = SimpleNamespace(mock=True, station="unused.yaml")
    service._lock = threading.Lock()
    service._closing = threading.Event()
    service.camera_state = "disconnected"
    service.camera_error = None
    service.video_backend = None
    service._camera_generation = 0
    service._camera_workers = []
    service.camera_slots = [
        SimpleNamespace(role=role, worker=None) for role in ("top", "left", "right")
    ]
    service.log = lambda message: None
    return service


def test_camera_import_failure_finishes_and_explicit_retry_works(monkeypatch):
    service = camera_service()
    original_import = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name == "camera.worker":
            raise ImportError("camera dependency unavailable")
        return original_import(name, *args, **kwargs)

    with monkeypatch.context() as failure:
        failure.setattr(builtins, "__import__", unavailable)
        service.connect_cameras()
        service.camera_thread.join(2)
    assert not service.camera_thread.is_alive()
    assert service.camera_state == "fault"
    assert service.camera_error == "camera dependency unavailable"
    assert all(slot.worker is None for slot in service.camera_slots)

    from yam_abc_reproduce import config, runtime
    from yam_abc_reproduce.camera import worker

    drivers = [SimpleNamespace(role=slot.role, serial=slot.role) for slot in service.camera_slots]
    started = []

    class FakeCameraWorker:
        def __init__(self, driver):
            self.role = driver.role

        def start(self):
            started.append(self.role)

    monkeypatch.setattr(
        config, "build_station_config", lambda path: SimpleNamespace(cameras=drivers)
    )
    monkeypatch.setattr(runtime, "build_cameras_from_config", lambda cfg, mock: drivers)
    monkeypatch.setattr(worker, "CameraWorker", FakeCameraWorker)
    assert not started  # Fixing the dependency does not retry by itself.
    service.connect_cameras()
    service.camera_thread.join(2)
    assert service.camera_state == "connected"
    assert service.camera_error is None
    assert started == ["top", "left", "right"]


def test_camera_thread_start_failure_is_visible(monkeypatch):
    service = camera_service()

    def unavailable(thread):
        raise RuntimeError("cannot start new thread")

    monkeypatch.setattr(threading.Thread, "start", unavailable)
    with pytest.raises(RuntimeError, match="cannot start"):
        service.connect_cameras()
    assert service.camera_state == "fault"
    assert service.camera_error == "cannot start new thread"


def test_initialization_failure_allows_explicit_backend_retry(monkeypatch, tmp_path):
    from yam_abc_reproduce import resource_qos
    from yam_abc_reproduce.hil import run

    service = Workbench.__new__(Workbench)
    service.args = SimpleNamespace(
        mock=True, station="unused.yaml", url=None, baseline=False, output=str(tmp_path)
    )
    service.mode = "collect"
    service._lock = threading.Lock()
    service.thread = None
    service.selected_task = None
    service.cleanup_error = None
    service.recording_recovery = {"state": "failed", "error": "previous session error"}
    service.log = lambda message: None
    attempts = []

    def fail(argv, service):
        attempts.append(argv)
        raise RuntimeError("initialization failed")

    monkeypatch.setattr(run, "main", fail)
    monkeypatch.setattr(resource_qos, "place_on_cpus", lambda kind: None)
    service.connect(ready=True, initialize=True)
    assert service.recording_recovery is None
    service.thread.join(2)
    assert service.state == "fault" and not service.initializing
    assert len(attempts) == 1
    service.connect(ready=True, initialize=True)
    service.thread.join(2)
    assert len(attempts) == 2


def test_real_frontend_initialization_recovery_gates():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the actual browser renderer")
    source = Path(__file__).resolve().parents[1] / "yam_abc_reproduce/hil/static/app.js"
    script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const nodes = new Map();
function element() {
  return {value:'', style:{}, dataset:{}, parentElement:{},
    classList:{toggle(){}, add(){}, remove(){}}, querySelector(){return element()},
    querySelectorAll(){return []}, append(){}, replaceChildren(){}, setAttribute(){}};
}
function get(id) {if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id)}
const context = {sessionStorage:{getItem(){return 'test'},setItem(){}},
  crypto:{randomUUID(){return 'test'}}, Date,
  document:{getElementById:get,querySelectorAll(){return []},querySelector(){return element()},
    createElement:element,createTextNode:x=>x}};
vm.createContext(context);
const source = fs.readFileSync(process.argv[1], 'utf8');
vm.runInContext(source.slice(0, source.indexOf('async function poll()')) + '\n' +
  source.slice(source.indexOf('function renderTask('), source.indexOf('$("connect-cameras").onclick')),
  context);
const ready = {connection:'disconnected',camera_connection:'connected',phase:'hold',mock:true,control_age_s:0.01,
  cameras:[{healthy:true},{healthy:true},{healthy:true}],initialization:{preflight:{ok:true}}};
function render(changes={}, online=true) {
  vm.runInContext(`state=${JSON.stringify({...ready,...changes})};online=${online};render();`, context);
}
for (const connection of ['disconnected','fault']) {
  render({connection}); assert.equal(get('init-arms').disabled, false, connection);
}
for (const connection of ['connecting','connected','disconnecting','finalizing']) {
  render({connection}); assert.equal(get('init-arms').disabled, true, connection);
}
render({connection:'fault',cleanup_error:'close failed'});
assert.equal(get('init-arms').disabled, true);
render({connection:'fault',initialization:{preflight:{ok:false}}});
assert.equal(get('init-arms').disabled, true);
render({connection:'fault'}, false); assert.equal(get('init-arms').disabled, true);
render({camera_connection:'connecting'});
assert.equal(get('connect-cameras').disabled, true);
assert.equal(get('init-cameras').disabled, true);
render({camera_connection:'fault',camera_error:'dependency unavailable',cameras:[]});
assert.equal(get('connect-cameras').disabled, false);
assert.equal(get('init-cameras').disabled, false);
assert.equal(get('init-arms').disabled, true);
render({camera_connection:'fault',camera_cleanup_pending:true,cameras:[]});
assert.equal(get('init-cameras').disabled, false);
assert.equal(get('init-cameras').textContent, '重试断开相机');
const cameraFunction = source.slice(source.indexOf('function cameraAction()'),
  source.indexOf('$("init-cameras").onclick'));
vm.runInContext(cameraFunction, context);
vm.runInContext('action = path => path', context);
assert.equal(vm.runInContext('cameraAction()', context), '/cameras/disconnect');
render({camera_connection:'fault',camera_cleanup_pending:false,cameras:[]});
assert.equal(vm.runInContext('cameraAction()', context), '/cameras/connect');
render({connection:'connected',mode:'teleop',recording_error:'writer failed',maintenance:'idle'});
assert.equal(get('recording-restart').hidden, false);
assert.equal(get('recording-restart').disabled, false);
for (const extra of [{intervention_pending:true},{recording:true},{task_switching:true},
  {recording_recovery:{state:'recovering'}}]) {
  render({connection:'connected',mode:'hil',recording_error:'writer failed',...extra});
  assert.equal(get('recording-restart').disabled, true);
}
render({connection:'connected',mode:'hil',recording_error:null,recording_recovery:{state:'complete'}});
assert.equal(get('recording-restart').hidden, true);
render({intervention_id:6,session_intervention_count:0});
assert.equal(get('interventions').textContent, 0);
render({intervention_id:7,session_intervention_count:1});
assert.equal(get('interventions').textContent, 1);
render({intervention_id:6,session_intervention_count:null});
assert.equal(get('interventions').textContent, '—');
"""
    result = subprocess.run(
        [node, "-e", script, str(source)], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
