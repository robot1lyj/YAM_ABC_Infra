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
