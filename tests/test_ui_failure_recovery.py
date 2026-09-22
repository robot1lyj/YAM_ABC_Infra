"""Recovery paths without robot, camera, CAN or model access."""

import builtins
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from yam_abc_reproduce.hil.workbench import Workbench


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
"""
    result = subprocess.run(
        [node, "-e", script, str(source)], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
