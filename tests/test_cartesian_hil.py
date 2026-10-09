"""Keyboard HIL contract: no motors, no network policy or background SDK writes."""

import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from yam_abc_reproduce.hil.cartesian_jog import CartesianJog
from yam_abc_reproduce.hil.core import Arbiter, Mode, Phase


def test_keyboard_ui_input_scope_and_focus_pause():
    """Exercise shipped handlers, not copies: no hidden-page jogs or implicit resume."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node required for UI event regression")
    source = Path(__file__).parents[1] / "yam_abc_reproduce/hil/static/app.js"
    script = r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const calls = [], nodes = new Map(), handlers = {}, windowHandlers = {};
let dialog = false;
function get(id) {
  if (!nodes.has(id)) nodes.set(id, {value:'2',hidden:false,
    click(){calls.push({path:'click:' + id})}});
  return nodes.get(id);
}
const context = {sessionStorage:{getItem(){return 'test'},setItem(){}},
  crypto:{randomUUID(){return 'test'}}, Date,
  document:{visibilityState:'visible',hidden:false,getElementById:get,
    querySelectorAll(){return []},querySelector(){return dialog ? {} : null},
    addEventListener(name,handler){handlers[name]=handler}},
  window:{addEventListener(name,handler){windowHandlers[name]=handler}}};
vm.createContext(context);
const source = fs.readFileSync(process.argv[1], 'utf8');
vm.runInContext(source.slice(0, source.indexOf('function text(')) + '\n' +
  source.slice(source.indexOf('function switchPage(next)'), source.indexOf('for (let j = 0;')) + '\n' +
  source.slice(source.indexOf('document.addEventListener("keydown"'), source.indexOf('$("hil-input-form").onsubmit')) + '\n' +
  source.slice(source.indexOf('window.addEventListener("blur"'), source.indexOf('for (const img')), context);
context.capture = (path,body) => calls.push({path,body});
vm.runInContext('action = async (path,body) => {capture(path,body);return true}; render = () => {}; text = () => {};', context);
const ready = {connection:'connected',mode:'hil',hil_input:'keyboard',phase:'human',
  epoch:5,tick:100,maintenance:'idle',cartesian:{last_command_id:10}};
function reset(changes={}) {
  calls.length=0; dialog=false;
  context.document.visibilityState='visible'; context.document.hidden=false;
  vm.runInContext(`state=${JSON.stringify({...ready,...changes})};online=true;page='workspace';
    keyboardArm='left';keyboardSuspendedEpoch=null;cartesianBusy=false;cartesianSerial=0;`, context);
}
async function key(name,extra={}) {
  let prevented=false;
  handlers.keydown({key:name,target:{tagName:'DIV'},preventDefault(){prevented=true},...extra});
  await Promise.resolve();
  return prevented;
}
async function main() {
  const keys={ArrowUp:['x',.002],ArrowDown:['x',-.002],ArrowLeft:['y',-.002],
    ArrowRight:['y',.002],PageUp:['z',.002],PageDown:['z',-.002],'[':['gripper',-.05],']':['gripper',.05]};
  for (const arm of ['left','right']) for (const [name,[axis,delta]] of Object.entries(keys)) {
    reset(); vm.runInContext(`keyboardArm='${arm}'`, context);
    assert.equal(await key(name),true);
    assert.deepEqual(calls.map(x=>JSON.parse(JSON.stringify(x))), [{path:'/hil/cartesian',body:{
      arm,axis,delta,epoch:5,command_id:11,observed_tick:100}}]);
  }
  for (const extra of [{ctrlKey:true},{altKey:true},{metaKey:true},{shiftKey:true},
    {repeat:true},{isComposing:true},{target:{tagName:'DIV',isContentEditable:true}},
    ...['INPUT','TEXTAREA','SELECT'].map(tagName=>({target:{tagName}}))]) {
    reset(); assert.equal(await key('ArrowDown',extra),false); assert.equal(calls.length,0);
  }
  for (const extra of [{ctrlKey:true},{altKey:true},{metaKey:true},{isComposing:true}]) {
    reset(); await key('s',extra); assert.equal(calls.length,0); // Browser/IME input never starts a model.
  }
  reset(); dialog=true; await key('PageDown'); assert.equal(calls.length,0);
  for (const page of ['devices','rl']) {
    reset(); vm.runInContext(`page='${page}'`,context);
    assert.equal(await key('ArrowDown'),false);
    await vm.runInContext("cartesianStep('right','z',-1)",context);
    assert.equal(calls.length,0); // Buttons use the same page gate.
  }
  reset(); context.document.visibilityState='hidden'; context.document.hidden=true;
  await key('PageDown'); assert.equal(calls.length,0);
  for (const changes of [{connection:'fault'},{phase:'hold'},{phase:'policy'},
    {hil_input:'leader'},{mode:'inference'},{stop_latched:true},{maintenance:'homing'},
    {task_switching:true},{initializing:true},{cartesian:{busy:true}},{cartesian:{warming:true}}]) {
    reset(changes); await key('PageDown'); assert.equal(calls.length,0,JSON.stringify(changes));
  }
  reset(); vm.runInContext('online=false',context); await key('PageDown'); assert.equal(calls.length,0);
  reset(); vm.runInContext('cartesianBusy=true',context);
  await key('PageDown'); assert.equal(calls.length,0);
  await key(' '); assert.equal(calls[0].path,'/event/hold'); // Pause is not gated by an in-flight jog.
  reset(); vm.runInContext("switchPage('workspace')",context); assert.equal(calls.length,0);
  vm.runInContext("switchPage('devices');switchPage('workspace')",context);
  assert.equal(calls.length,1); assert.equal(calls[0].path,'/event/hold');
  await key('PageDown'); assert.equal(calls.length,1); // Returning before HOLD acknowledgement stays blocked.
  reset(); windowHandlers.blur();
  context.document.visibilityState='hidden';context.document.hidden=true;handlers.visibilitychange();
  assert.equal(calls.length,1);assert.equal(calls[0].path,'/event/hold'); // Blur + hidden is one pause.
  context.document.visibilityState='visible';context.document.hidden=false;
  await key('PageDown'); assert.equal(calls.length,1);
  vm.runInContext("state.phase='hold';state.epoch=6",context);
  await key('PageDown'); assert.equal(calls.length,1);
  vm.runInContext("state.phase='human';state.epoch=7",context); // Explicit new takeover/continue.
  await key('PageDown'); assert.equal(calls.length,2);assert.equal(calls[1].path,'/hil/cartesian');
  reset();context.document.visibilityState='hidden';context.document.hidden=true;
  handlers.visibilitychange();assert.equal(calls[0].path,'/event/hold'); // Hidden event still pauses.
  reset({hil_input:'leader'});windowHandlers.blur();handlers.visibilitychange();assert.equal(calls.length,0);
}
main().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run([node, "-e", script, str(source)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


class FakeArm:
    def fk(self, q):
        pose = np.eye(4)
        pose[:3, 3] = q[:3]
        return pose

    def ik(self, pose, seed):
        q = seed.copy()
        q[:3] = pose[:3, 3]
        return q


def fake_kinematics():
    return SimpleNamespace(left=FakeArm(), right=FakeArm())


def wait_result(jog, epoch=3):
    for _ in range(100):
        target = jog.poll(epoch, time.monotonic())
        if target is not None or jog.error:
            return target
        time.sleep(.005)
    pytest.fail("test worker did not finish")


def request(jog, arm="left", axis="z", delta=-.001, **extra):
    jog.validate(arm, axis, delta)
    jog.prepare()
    assert jog.ready.wait(1)
    q = np.zeros(14)
    q[[6, 13]] = .5
    jog.request(arm=arm, axis=axis, delta=delta, epoch=3, command_id=1,
                target=q, measured=q, now=time.monotonic(), **extra)
    return q


@pytest.mark.parametrize("arm,axis,index", [(a, xyz, i + offset)
    for a, offset in (("left", 0), ("right", 7)) for i, xyz in enumerate("xyz")])
def test_each_arm_axis_changes_only_selected_target(arm, axis, index):
    jog = CartesianJog(fake_kinematics)
    try:
        q = request(jog, arm, axis, .002)
        target = wait_result(jog)
        expected = q.copy()
        expected[index] += .002
        np.testing.assert_allclose(target, expected)
        np.testing.assert_allclose(np.array(jog.applied["target_pose"])[:3, :3], np.eye(3))
    finally:
        jog.close()


@pytest.mark.parametrize("arm,index", [("left", 6), ("right", 13)])
def test_gripper_step_keeps_all_joints_and_other_arm(arm, index):
    def missing_ik():
        raise ValueError("no IK installed")
    jog = CartesianJog(missing_ik)
    try:
        q = request(jog, arm, "gripper", -.05)
        target = wait_result(jog)
        q[index] -= .05
        np.testing.assert_allclose(target, q)
    finally:
        jog.close()


@pytest.mark.parametrize("axis,delta", [("x", .006), ("z", 0), ("y", float("nan")),
                                      ("gripper", -.11), ("roll", .001)])
def test_bad_steps_are_rejected(axis, delta):
    jog = CartesianJog(fake_kinematics)
    with pytest.raises(ValueError):
        request(jog, axis=axis, delta=delta)
    assert jog.thread is None


def test_invalid_feedback_and_closed_worker_are_rejected():
    jog = CartesianJog(fake_kinematics)
    q = np.zeros(14)
    bad = q.copy()
    bad[0] = np.nan
    with pytest.raises(ValueError, match="反馈无效"):
        jog.request(arm="left", axis="z", delta=.001, epoch=0, command_id=1,
                    target=q, measured=bad, now=time.monotonic())
    assert jog.thread is None
    jog.close()
    with pytest.raises(ValueError, match="会话已关闭"):
        jog.prepare()


@pytest.mark.parametrize("arm,axis", [(a, xyz) for a in ("left", "right") for xyz in "xyz"])
def test_official_yam_fk_ik_step_preserves_orientation_and_other_arm(arm, axis):
    from yam_abc_reproduce.hil.kinematics import DualArmEefConverter

    model = DualArmEefConverter()
    jog = CartesianJog(lambda: model)
    q = np.tile([.1, .4, .5, .2, .3, .1, .5], 2)
    offset = 0 if arm == "left" else 7
    other = 7-offset
    expected = getattr(model, arm).fk(q[offset:offset+6]).copy()
    expected["xyz".index(axis), 3] -= .001
    try:
        jog.prepare()
        assert jog.ready.wait(1)
        jog.request(arm=arm, axis=axis, delta=-.001, epoch=3, command_id=1,
                    target=q, measured=q, now=time.monotonic())
        target = wait_result(jog)
        assert target is not None, jog.error
        actual = getattr(model, arm).fk(target[offset:offset+6])
        np.testing.assert_allclose(actual[:3, 3], expected[:3, 3], atol=2e-4)
        np.testing.assert_allclose(actual[:3, :3], expected[:3, :3], atol=2e-4)
        np.testing.assert_array_equal(target[other:other+7], q[other:other+7])
    finally:
        jog.close()


def test_single_inflight_cancel_rejects_old_result_and_duplicate_id():
    entered, release = threading.Event(), threading.Event()

    class SlowArm(FakeArm):
        def ik(self, pose, seed):
            entered.set()
            release.wait(1)
            return super().ik(pose, seed)

    jog = CartesianJog(lambda: SimpleNamespace(left=SlowArm(), right=SlowArm()))
    try:
        q = request(jog)
        assert entered.wait(1)
        with pytest.raises(ValueError, match="上一小步"):
            jog.request(arm="right", axis="x", delta=.001, epoch=3, command_id=2,
                        target=q, measured=q, now=time.monotonic())
        jog.cancel()
        release.set()
        time.sleep(.02)
        assert jog.poll(4, time.monotonic()) is None
        assert jog.applied is None
        with pytest.raises(ValueError, match="过期"):
            request(jog)
    finally:
        release.set()
        jog.close()


def test_singular_jump_and_expired_solution_are_never_applied():
    class JumpArm(FakeArm):
        def ik(self, pose, seed):
            return seed + .2

    jog = CartesianJog(lambda: SimpleNamespace(left=JumpArm(), right=JumpArm()))
    try:
        request(jog)
        assert wait_result(jog) is None
        assert "奇异" in jog.error
    finally:
        jog.close()
    jog = CartesianJog(fake_kinematics)
    try:
        request(jog)
        time.sleep(.02)
        assert jog.poll(3, time.monotonic() + 1) is None
        assert "超时" in jog.error
    finally:
        jog.close()


def test_keyboard_takeover_invalidates_policy_and_never_aligns_or_reads_leader():
    q, leader = np.zeros(14), np.zeros(14)
    leader[:6] = .5
    a = Arbiter(Mode.HIL, streaming=True)
    a.hil_input = "keyboard"
    a.start(q)
    old = a.request(1, 0)
    a.takeover(q, leader)
    assert a.phase == Phase.HUMAN and a.intervention_pending
    assert not a.intervention_waiting and a._alignment is None
    assert a.pending is None and not a.accept(old, np.zeros((50, 14)), .1)
    d = a.step(q, leader+0.1, now=.1, dt=1/30)
    np.testing.assert_allclose(d.action, q)
    np.testing.assert_allclose(d.leader_hold_target, leader)
    assert d.intervention and d.leader_freeze and not d.leader_manual
    a.hold(q)
    a.start(q, leader)
    assert a.phase == Phase.HOLD  # Start cannot bypass handback.
    a.takeover(q, leader)
    assert a.phase == Phase.HUMAN
    a.resume_policy(q)
    assert a.phase == Phase.RESUME and not a.intervention_pending
    d = a.step(q, leader, now=.2, dt=1/30)
    assert d.leader_freeze


def test_keyboard_web_contract_validation_without_hardware():
    from fastapi.testclient import TestClient

    from yam_abc_reproduce.hil.web import create_app

    calls = []
    runtime = SimpleNamespace(status={}, configure_hil_input=lambda **kw: calls.append(kw),
                              request_cartesian=lambda **kw: calls.append(kw))
    with TestClient(create_app(runtime)) as client:
        headers = {"X-YAM-Control": "1"}
        assert client.post("/hil/input", json={"input": "keyboard"}, headers=headers).status_code == 200
        body = dict(arm="right", axis="z", delta=-.001, epoch=3, command_id=1, observed_tick=7)
        assert client.post("/hil/cartesian", json=body, headers=headers).status_code == 200
        assert calls[-1] == body
        assert client.post("/hil/cartesian", json=dict(body, epoch=True), headers=headers).status_code == 422
        assert client.post("/hil/cartesian", json=body).status_code == 403


def test_runtime_records_dual_arm_keyboard_actions_and_preserves_leaders(tmp_path):
    from yam_abc_reproduce.camera.mock_camera import MockCamera
    from yam_abc_reproduce.camera.worker import CameraWorker
    from yam_abc_reproduce.config import StationConfig
    from yam_abc_reproduce.hil.policy import PolicyWorker
    from yam_abc_reproduce.hil.recording import Recorder
    from yam_abc_reproduce.hil.run import MockPolicy, Runtime
    from yam_abc_reproduce.hil.station import StationIO
    from yam_abc_reproduce.hil.storage import read_rows
    from yam_abc_reproduce.runtime import build_arm_units

    io = StationIO(build_arm_units(StationConfig(), mock=True), mock=True)
    rec = Recorder(tmp_path / "keyboard")
    cams = [CameraWorker(MockCamera(r, r, width=32, height=32)) for r in ("top", "left", "right")]
    worker = PolicyWorker(MockPolicy(), auto_connect=True)
    runtime = Runtime(io, cams, worker, rec, mode="hil", settings={"policy_fusion": "raw"})
    runtime.cartesian.factory = fake_kinematics
    runtime.cartesian.prepare()
    runtime.session.arbiter.hil_input = "keyboard"
    thread = threading.Thread(target=runtime.run, kwargs={"duration": 3, "auto_start": True})

    def until(predicate):
        for _ in range(200):
            if predicate():
                return
            time.sleep(.01)
        pytest.fail(f"runtime condition failed: {runtime.status}")

    try:
        for c in cams:
            c.start()
        thread.start()
        until(lambda: runtime.status.get("phase") == "policy")
        runtime.event("takeover")
        until(lambda: runtime.status.get("phase") == "human")
        assert runtime.status["intervention_id"] == 1
        frozen = runtime.status["leader_state"].copy()
        for identity, epoch, tick in ((99, runtime.status["epoch"]-1, runtime.status["tick"]),
                                     (99, runtime.status["epoch"], runtime.status["tick"]-100)):
            with pytest.raises(ValueError):
                runtime.request_cartesian(arm="left", axis="z", delta=-.001,
                    epoch=epoch, command_id=identity, observed_tick=tick)
        for identity, arm in enumerate(("left", "right"), 1):
            runtime.request_cartesian(arm=arm, axis="z", delta=-.001,
                epoch=runtime.status["epoch"], command_id=identity, observed_tick=runtime.status["tick"])
            until(lambda: (runtime.status.get("cartesian", {}).get("applied") or {}).get("command_id") == identity)
        runtime.event("hold")
        until(lambda: runtime.status.get("phase") == "hold")
        assert runtime.status["cartesian"]["applied"] is None
        with pytest.raises(ValueError, match="介入"):
            runtime.request_cartesian(arm="left", axis="z", delta=-.001,
                epoch=runtime.status["epoch"], command_id=3, observed_tick=runtime.status["tick"])
        np.testing.assert_allclose(runtime.status["leader_state"], frozen)
    finally:
        runtime.stopping.set()
        thread.join(4)
        for c in cams:
            c.stop()
        rec.close()
        worker.close()
        io.close()
    assert not runtime.status.get("error")
    rows = list(read_rows(rec.path))
    manual = [r for r in rows if r["expert_valid"]]
    assert manual and all(r["human_input"]["mode"] == "keyboard" for r in manual)
    assert {r["human_input"]["command"]["arm"] for r in manual} == {"left", "right"}
    for row in manual:
        np.testing.assert_allclose(row["human_action"], row["submitted_action"])
