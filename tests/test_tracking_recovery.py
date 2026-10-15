"""Explicit slow Follower recovery, without clearing the policy safety latch."""
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

from yam_abc_reproduce.config import StationConfig
from yam_abc_reproduce.hil.maintenance import Maintenance
from yam_abc_reproduce.hil.recording import RecordingSession
from yam_abc_reproduce.hil.run import Runtime
from yam_abc_reproduce.hil.station import StationIO
from yam_abc_reproduce.runtime import build_arm_units


def recovery_runtime(tmp_path):
    io = StationIO(build_arm_units(StationConfig(), mock=True, followers_only=True), mock=True)
    q = np.zeros(7)
    q[4], q[6] = .08, .6
    io.units[1].robot.command_joint_pos(q)
    rec = RecordingSession(tmp_path / "recovery", mode="teleop", defer_storage=True)
    runtime = Runtime(io, [NS(role=r, history=lambda: []) for r in ("top", "left", "right")],
                      None, rec, mode="inference")
    runtime.maintenance = Maintenance(factory_zero=True, leaders_connected=False)
    fault = {"arm": "右 Follower", "joint": "J5", "message": "collision latch"}
    runtime.device_health.tracking_fault = fault
    return runtime, io, rec, fault


def test_runtime_recovery_home_completes_without_clearing_policy_latch(tmp_path):
    runtime, io, rec, fault = recovery_runtime(tmp_path)
    runtime.events.put(("home", time.monotonic()))
    try:
        status = runtime.run(duration=1)
        assert status["phase"] == "hold" and status["maintenance"] == "idle"
        assert abs(io.units[1].robot.get_joint_pos()[4]) <= .015
        assert io.units[1].robot.get_joint_pos()[6] == .6
        assert runtime.device_health.tracking_fault is fault
        with pytest.raises(ValueError, match="collision latch"):
            runtime.event("start")
    finally:
        rec.close()
        io.close()


def test_explicit_recovery_ack_clears_latch_without_homing_or_starting(tmp_path):
    runtime, io, rec, _ = recovery_runtime(tmp_path)
    before = [unit.robot.get_joint_pos().copy() for unit in io.units]
    epoch = runtime.session.arbiter.epoch
    runtime.event("ack_device_health")
    try:
        status = runtime.run(duration=.15)
        assert status["phase"] == "hold" and status["maintenance"] == "idle"
        assert not status["device_health"]["tracking_fault"]
        assert status["device_health"]["ok"]
        assert runtime.session.arbiter.epoch == epoch
        assert not rec.recording and runtime.events.empty()
        for unit, pose in zip(io.units, before):
            np.testing.assert_array_equal(unit.robot.get_joint_pos(), pose)
    finally:
        rec.close()
        io.close()


def test_recovery_home_stops_pushing_when_still_blocked(tmp_path, monkeypatch):
    runtime, io, rec, fault = recovery_runtime(tmp_path)
    robot = io.units[1].robot
    robot._pos[4] = .5
    submitted = []
    def blocked(q):
        submitted.append(q.copy())
    robot.command_joint_pos = blocked
    monkeypatch.setattr(Maintenance, "HOME_STALL_TIMEOUT", .1)
    runtime.events.put(("home", time.monotonic()))
    try:
        status = runtime.run(duration=1.5)
        assert status["phase"] == "hold" and status["maintenance"] == "idle"
        assert "停滞" in status["maintenance_error"]
        assert min(q[4] for q in submitted) >= .5 - Maintenance.HOME_TRACKING_WINDOW - 1e-9
        np.testing.assert_allclose(submitted[-1], robot.get_joint_pos())
        assert runtime.device_health.tracking_fault is fault
    finally:
        rec.close()
        io.close()


@pytest.mark.parametrize("event", ["hold", "stop"])
def test_recovery_home_can_always_be_paused_or_emergency_stopped(tmp_path, event):
    runtime, io, rec, _ = recovery_runtime(tmp_path)
    runtime.events.put(("home", time.monotonic()))
    thread = threading.Thread(target=runtime.run)
    thread.start()
    try:
        deadline = time.monotonic() + 2
        while runtime.status.get("maintenance") != "homing" and time.monotonic() < deadline:
            time.sleep(.01)
        assert runtime.status["maintenance"] == "homing"
        runtime.event(event)
        while runtime.status.get("maintenance") != "idle" and time.monotonic() < deadline:
            time.sleep(.01)
        assert runtime.status["maintenance"] == "idle" and runtime.status["phase"] == "hold"
        assert runtime.status["stop_latched"] == (event == "stop")
        held = io.units[1].robot.get_joint_pos()
        time.sleep(.08)
        np.testing.assert_array_equal(io.units[1].robot.get_joint_pos(), held)
    finally:
        runtime.stopping.set()
        thread.join(3)
        rec.close()
        io.close()


def test_ui_recovery_home_distinguishes_a_latch_from_actual_feedback_failure():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node UI checks run on development host")
    source = Path("yam_abc_reproduce/hil/static/app.js").read_text()
    marker = "const factoryZero = !!state.factory_zero_home;"
    code = marker + source.split(marker, 1)[1].split('  $("gravity").disabled', 1)[0]
    script = """
    const vm=require('node:vm'), assert=require('node:assert/strict'), code=JSON.parse(process.argv[1]);
    function render(changes={}) {
      const nodes={}, state={factory_zero_home:true, home_available:true, leaders_connected:true,
        device_health:{ok:false, feedback_ok:true, tracking_fault:{}, reason:'collision latch'}, ...changes.state};
      const {state:ignored,...flags}=changes;
      const context={state, connected:true, latched:false, paused:true, recording:false, idle:true,
        canMaintain:true, ...flags, $:id=>nodes[id] ||= {}, text:()=>{}};
      vm.runInNewContext(code,context); return nodes;
    }
    let n=render(); assert.equal(n.home.disabled,false); assert.equal(n['home-leader'].disabled,true);
    n=render({state:{device_health:{ok:false, feedback_ok:false, feedback_reason:'motor alarm'}}});
    assert.equal(n.home.disabled,true); assert.equal(n.home.title,'motor alarm');
    for(const state of [{device_health:undefined},{factory_zero_home:false}])
      assert.equal(render({state}).home.disabled,true);
    for(const changes of [{connected:false},{latched:true},{paused:false},{recording:true},{idle:false}])
      assert.equal(render(changes).home.disabled,true);
    """
    subprocess.run([node, "-e", script, json.dumps(code)], check=True, capture_output=True, text=True)


def test_visible_recovery_buttons_share_current_health_guards_and_do_not_start_motion():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node UI checks run on development host")
    source = Path("yam_abc_reproduce/hil/static/app.js").read_text()
    helpers = "function activeDevice() {" + source.split("function activeDevice() {", 1)[1].split("function canDeleteEpisode()", 1)[0]
    handler = "function confirmTrackingRecovery() {" + source.split("function confirmTrackingRecovery() {", 1)[1].split("let renderedPldMode", 1)[0]
    script = """
    const assert=require('node:assert/strict'), nodes=new Map();
    const $=id=>{if(!nodes.has(id))nodes.set(id,{hidden:true,open:false,close(){this.open=false}});return nodes.get(id)};
    const text=(id,value)=>$(id).textContent=value;
    let online=true;
    const state={connection:'connected',control_age_s:.01,phase:'hold',maintenance:'idle',stop_latched:false,
      device_health:{feedback_ok:true,tracking_fault:{arm:'右 Follower',joint:'J4',error_deg:21.9}}};
    let confirmation,requests=[],messages=[];
    const confirmAction=(title,copy,callback)=>{confirmation={title,copy,callback}};
    const toast=message=>messages.push(message);
    const action=async path=>{requests.push(path);state.device_health.tracking_fault=null;return true};
    """ + helpers + handler + """
    assert.equal(renderTrackingRecovery(),true);
    for(const id of ['tracking-recover','device-health-ack']) {
      assert.equal($(id).hidden,false);assert.equal($(id).disabled,false);
    }
    assert.match($('tracking-recovery-summary').textContent,/右 Follower J4.*21.9/);
    assert.match($('tracking-recovery-hint').textContent,/不是恢复的前提/);
    for(const [key,value] of [['phase','policy'],['maintenance','homing'],['maintenance','gravity'],
      ['stop_latched',true],['connection','disconnected'],['control_age_s',1]]) {
      const previous=state[key];state[key]=value;renderTrackingRecovery();
      assert.equal($('tracking-recover').disabled,true);state[key]=previous;
    }
    online=false;renderTrackingRecovery();assert.equal($('tracking-recover').disabled,true);online=true;
    state.device_health.feedback_ok=false;state.device_health.feedback_reason='右 J4 电机报警';
    renderTrackingRecovery();assert.equal($('tracking-recover').disabled,true);
    assert.match($('tracking-recover').title,/电机报警/);
    state.device_health.feedback_ok=true;state.device_health.feedback_reason=null;
    assert.equal($('tracking-recover').onclick,$('device-health-ack').onclick);
    $('tracking-recover').onclick();
    assert.deepEqual(requests,[]); // Merely displaying a confirmation never clears the latch.
    assert.match(confirmation.copy,/无需先回零/);
    (async()=>{
      state.stop_latched=true;await confirmation.callback();assert.deepEqual(requests,[]);
      state.stop_latched=false;await confirmation.callback();
      assert.deepEqual(requests,['/event/ack_device_health']); // No home, start, resume or SDK operation.
      assert.equal(state.phase,'hold');renderTrackingRecovery();
      assert.equal($('tracking-recovery').hidden,true);
      assert.match(messages.at(-1),/已解除.*仍保持暂停/);
    })().catch(error=>{console.error(error);process.exit(1)});
    """
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
    html = Path("yam_abc_reproduce/hil/static/index.html").read_text()
    assert html.count('id="tracking-recover"') == 1
    assert 'id="tracking-recovery"' in html.split('id="alert"', 1)[1].split('<div id="workspace-page">', 1)[0]


def test_alert_separates_historical_latch_current_alarm_and_success_notice():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node UI checks run on development host")
    source = Path("yam_abc_reproduce/hil/static/app.js").read_text()
    helpers = "function activeDevice() {" + source.split("function activeDevice() {", 1)[1].split("function canDeleteEpisode()", 1)[0]
    acknowledgement = "const trackingAcknowledged = " + source.split("const trackingAcknowledged = ", 1)[1].split(";", 1)[0] + ";"
    alerts = "const errors = [" + source.split("  const errors = [", 1)[1].split('  $("episodes").replaceChildren();', 1)[0]
    script = """
    const assert=require('node:assert/strict'), nodes=new Map();
    const $=id=>{if(!nodes.has(id))nodes.set(id,{});return nodes.get(id)};
    const text=(id,value)=>$(id).textContent=value, operatorHint=value=>value;
    const connected=true, online=true, mode='inference',pldMode='off',pld={};
    const state={connection:'connected',control_age_s:.01,phase:'hold',maintenance:'idle',
      device_health:{feedback_ok:true,feedback_reason:null,reason:'old fault; please home first',
        tracking_fault:{arm:'右 Follower',joint:'J4',error_deg:21.9}}};
    """ + helpers + "function renderAlerts(){" + acknowledgement + alerts + "}" + """
    renderAlerts();
    assert.equal($('alert').hidden,false);assert.equal($('alert-message').hidden,true);
    assert.doesNotMatch($('tracking-recovery-hint').textContent,/please home first/);
    state.device_health.feedback_ok=false;state.device_health.feedback_reason='SDK state update stale';
    renderAlerts();assert.equal($('alert-message').hidden,false);
    assert.match($('alert-message').textContent,/SDK state update stale/);
    state.device_health={ok:true,feedback_ok:true,tracking_fault:null,reason:null};
    state.operator_error='已确认；未自动恢复运动。静止检查不证明关节跟随已恢复。';
    renderAlerts();assert.equal($('alert').hidden,true); // Successful ack is not another red error.
    state.operator_error='真正的执行错误';renderAlerts();
    assert.equal($('alert').hidden,false);assert.match($('alert-message').textContent,/真正的执行错误/);
    """
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
