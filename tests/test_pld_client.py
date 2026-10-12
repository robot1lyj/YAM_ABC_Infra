"""PLD-Grasp client contract and native recording, without an RL learner/robot."""

import copy
import json
import threading
import time
from collections import deque
from types import SimpleNamespace

import h5py
import numpy as np
import pytest
from fastapi.testclient import TestClient

from yam_abc_reproduce.camera.mock_camera import MockCamera
from yam_abc_reproduce.camera.worker import CameraWorker
from yam_abc_reproduce.config import StationConfig
from yam_abc_reproduce.hil.core import Arbiter, Mode, Request
from yam_abc_reproduce.hil.pld import MODES, SCHEMA, PldClient, PldConfig, validate_reply
from yam_abc_reproduce.hil.policy import PolicyWorker, Reply, RtcJob
from yam_abc_reproduce.hil.policy_process import ProcessPolicyClient
from yam_abc_reproduce.hil.recording import RecordingSession
from yam_abc_reproduce.hil.recording_service import RemoteRecordingSession
from yam_abc_reproduce.hil.rtc_protocol import build_rtc_request
from yam_abc_reproduce.hil.rtc_timeline import RtcCommitment
from yam_abc_reproduce.hil.run import Runtime
from yam_abc_reproduce.hil.session import Session
from yam_abc_reproduce.hil.station import StationIO
from yam_abc_reproduce.hil.storage import read_rows
from yam_abc_reproduce.hil.web import create_app
from yam_abc_reproduce.runtime import build_arm_units


def metadata():
    return dict(rtc_mode="trained", rtc_max_delay_steps=10, action_horizon=50,
        action_dim=14, action_dt_s=1/30, pld=dict(schema=SCHEMA, expert="PLD-Grasp",
        modes=list(MODES), contract_sha256="a"*64, snapshot_sha256="b"*64))


def observation():
    image = np.zeros((8, 8, 3), np.uint8)
    return {"observation.state": np.full(14, .7), "prompt": "sort blocks",
            **{f"observation.images.{r}_rgb": image for r in ("top", "left", "right")}}


def request(mode="probe"):
    client = PldClient(dict(mode=mode, run_id="experiment"))
    client.prepare(metadata(), rtc=True)
    token = Request(3, 123, 456, 1.0, .98, observation_policy_tick=30, rtc_delay_steps=9)
    payload = client.build_request(token, episode_id="original-episode", previous_command=np.full(14, .2))
    return client, token, payload


def response(payload, base=None, active=True):
    base = np.zeros((50, 14)) if base is None else np.array(base, copy=True)
    trace = {**payload, "base": base.copy(), "state": np.full(14, .7),
        "gate": dict(active=active, arm="left" if active else None),
        "start_tick": 30, "delay_steps": 9,
        "decision_id": f"e{payload['epoch']}:r{payload['request_id']}",
        "unknown_future_trace": {"array": np.arange(5, dtype=np.float64), "nested": ["keep", {"x": 9}]} }
    if active:
        trace.update(arm="left", window_id="window-from-thor", x=np.arange(32, dtype=np.float32),
                     sample_u=np.zeros(7), scale=np.ones(7), action=np.zeros(7), actions=base.copy())
    return {"actions": base, "pld": trace, "server_timing": {"rtc_used": True}}


@pytest.mark.parametrize("mode", MODES)
def test_modes_and_request_use_native_token_previous_command(mode):
    client, token, payload = request(mode)
    if mode == "off":
        assert payload == {"mode": "off"}
        return
    assert payload["schema"] == SCHEMA
    assert payload["epoch"] == token.epoch and payload["request_id"] == token.request_id
    assert payload["observation_id"] == token.observation_id
    assert payload["episode_id"] == "original-episode"
    np.testing.assert_array_equal(payload["previous_command"], np.full(14, .2))
    assert not np.array_equal(payload["previous_command"], observation()["observation.state"])
    wire = build_rtc_request(observation(), target_start_tick=30,
        committed_actions=np.zeros((9, 14)), max_delay_steps=10, pld=payload)
    assert set(wire) == {"type", "obs", "rtc", "pld"}
    assert wire["obs"] is not None and wire["pld"] is payload
    assert not set(payload) & {"attempt_id", "arm", "elapsed_seconds"}


@pytest.mark.parametrize("bad", [{}, {"rtc_mode": "trained"}, {"rtc_mode": "off", "pld": metadata()["pld"]}])
def test_nonoff_fails_clearly_without_expert_or_trained_rtc(bad):
    client = PldClient({"mode": "probe"})
    with pytest.raises(ValueError, match="PLD|Thor"):
        client.prepare(bad, rtc=True)
    assert not client.snapshot(bad)["ready"]


def test_identity_pin_and_config_mismatch_cannot_silently_reload():
    client, _, _ = request()
    changed = metadata()
    changed["pld"]["snapshot_sha256"] = "c"*64
    with pytest.raises(ValueError, match="运行中变化"):
        client.prepare(changed, rtc=True)
    assert client.identity["snapshot_sha256"] == "b"*64
    with pytest.raises(ValueError, match="不匹配"):
        PldClient(dict(mode="probe", contract_sha256="c"*64)).prepare(metadata(), rtc=True)
    with pytest.raises(TypeError):
        PldConfig.from_dict({"mode": "probe", "selector": {"schema": "rules_auto_v2"}})
    assert PldClient({"mode": "probe"}).snapshot(metadata())["ready"]  # No first-start UI deadlock.


@pytest.mark.parametrize("mode", ["shadow", "probe"])
def test_nonexecuting_modes_reject_changed_actions(mode):
    _, _, payload = request(mode)
    reply = response(payload)
    validate_reply(payload, reply)
    reply["actions"][12, 0] += .001
    with pytest.raises(ValueError, match="必须执行原Pi"):
        validate_reply(payload, reply)


def test_probe_only_handshake_cannot_enable_actor_modes():
    meta = metadata()
    meta["pld"].update(modes=["off", "probe"], actor_loaded=False,
        phase="probe_diagnostics_only", replay_contract_ready=False)
    client = PldClient({"mode": "probe"})
    client.prepare(meta, rtc=True)
    status = client.snapshot(meta)
    assert status["ready"] and status["supported_modes"] == ["off", "probe"]
    assert status["replay_contract_ready"] is False
    for mode in ("shadow", "collect", "eval"):
        with pytest.raises(ValueError, match="仅支持 probe"):
            PldClient({"mode": mode}).prepare(meta, rtc=True)
    assert PldClient().snapshot({"pld": "broken"})["supported_modes"] == ["off"]


@pytest.mark.parametrize("field,value", [("state", np.zeros(14)), ("start_tick", 31), ("delay_steps", 8)])
def test_response_must_match_original_observation_and_rtc_ticks(field, value):
    _, token, payload = request()
    raw = response(payload)
    raw["pld"][field] = value
    with pytest.raises(ValueError, match="原请求|原RTC"):
        validate_reply(payload, raw, token=token, observation=observation())


def test_previous_command_is_exact_prior_submission_not_state_or_future():
    runtime = Runtime.__new__(Runtime)
    runtime._submitted_history = deque([(10, 1.01, np.full(14, .2)), (11, 1.04, np.full(14, .9))])
    previous, context = runtime.previous_command(11, 1.03)
    np.testing.assert_array_equal(previous, np.full(14, .2))
    assert context == {"previous_command_tick": 10, "previous_command_submitted_at": 1.01}
    assert runtime.previous_command(11, 1.0) is None
    assert runtime.previous_command(10, 1.03) is None


def wait_reply(worker):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        reply = worker.poll()
        if reply is not None:
            return reply
        time.sleep(.005)
    pytest.fail("policy worker did not return")


def test_worker_keeps_full_unknown_trace_when_rejected_and_single_flight():
    _, token, payload = request()
    raw = response(payload)
    raw["pld"]["snapshot_sha256"] = "c"*64
    client = SimpleNamespace(rtc=True, infer_rtc=lambda *a, **kw: raw)
    worker = PolicyWorker(client)
    commitment = RtcCommitment(30, 9, 39, np.zeros((9, 14)))
    try:
        assert worker.submit(token, RtcJob(observation(), commitment, payload, "original-session"))
        assert not worker.submit(token, RtcJob(observation(), commitment, payload))
        reply = wait_reply(worker)
        assert reply.error and reply.actions is None
        assert reply.pld is raw["pld"] and "unknown_future_trace" in reply.pld
        assert reply.recording_session == "original-session"
    finally:
        worker.close()


def test_cancelled_reply_keeps_trace_and_does_not_install_or_relabel_prefix():
    client, _, payload = request()
    arbiter = Arbiter(Mode.HIL, policy_fusion="rtc")
    q = np.zeros(14)
    arbiter.start(q)
    arbiter.rtc_timeline.record_submitted(0, q)
    token, commitment = arbiter.request_rtc(1, 0, 0, 0, 0, lambda a: a)
    raw = response(payload)
    reply = Reply(token, np.zeros((50, 14)), pld=raw["pld"], pld_request=payload)
    worker = SimpleNamespace(poll=lambda: reply, ready=True)
    session = Session(arbiter, worker, rtc_limit_target=lambda a: a)
    session.pld = client
    session.tick(q, q, now=.01, dt=1/30, observation_id=2, event="takeover", policy_tick=1)
    assert session.last_reply["discarded"] is True
    assert session.last_reply["pld"] is raw["pld"]
    assert not arbiter.rtc_timeline.has_accepted_plan


@pytest.mark.parametrize("remote", [False, True])
def test_original_episode_id_full_hdf_trace_late_reply_and_video_alignment(tmp_path, remote):
    recorder_type = RemoteRecordingSession if remote else RecordingSession
    recorder = recorder_type(tmp_path / "session", video_backend="libx264")
    _, token, payload = request()
    images = {r: np.zeros((32, 32, 3), np.uint8) for r in ("top", "left", "right")}
    recorder.start_episode()
    episode = recorder.episode_id
    payload["episode_id"] = episode
    raw = response(payload)
    detail = dict(token=token.__dict__, pld=raw["pld"], discarded=False, error=None)
    row = dict(tick=3, epoch=token.epoch, time=1.000000000123, obs_id=456,
        observation_valid=True, submitted_action=np.full(14, .2), policy_reply=detail,
        policy_selection=dict(request=token.__dict__, model_index=17, target_tick=47))
    assert recorder.submit(row, images)
    recorder.stop_episode()
    late = copy.deepcopy(detail)
    late["discarded"] = True
    assert recorder.record_policy_reply(dict(event="reply", episode_id=episode,
        details={"policy_reply": late}), session_path=recorder.path)
    recorder.close()
    directory = recorder.path / "episode_000001"
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["episode_id"] == episode
    rows = list(read_rows(directory))
    assert len(rows) == 1 and rows[0]["time"] == row["time"]
    assert rows[0]["policy_reply"]["pld"]["unknown_future_trace"]["array"] == list(range(5))
    assert rows[0]["video_indices"] == {r: 0 for r in images}
    assert manifest["segments"][0]["video_frames"] == {r: 1 for r in images}
    with h5py.File(directory / manifest["segments"][0]["path"] / "samples.h5") as h5:
        assert h5["time"].dtype == np.float64 and h5["submitted_action"].dtype == np.float64
        assert h5["submitted_action__valid"][0]
    event = json.loads((recorder.path / "policy_events.jsonl").read_text())
    assert event["episode_id"] == episode
    assert event["details"]["policy_reply"]["discarded"] is True
    assert event["details"]["policy_reply"]["pld"]["unknown_future_trace"]["array"] == list(range(5))


def test_process_transport_retains_pld_on_prefix_rejection():
    pytest.importorskip("openpi_client")
    from openpi_client import msgpack_numpy
    from websockets.sync.server import serve

    _, token, payload = request()

    def handler(ws):
        packer = msgpack_numpy.Packer()
        ws.send(packer.pack(metadata()))
        wire = msgpack_numpy.unpackb(ws.recv(timeout=3))
        assert wire["pld"]["request_id"] == token.request_id
        base = np.zeros((50, 14))
        base[:9] = wire["rtc"]["committed_actions"]
        raw = response(wire["pld"], base)
        raw["actions"][0, 0] += .1  # Rejected before installation, but raw trace must survive.
        ws.send(packer.pack(raw))
        try:
            ws.recv(timeout=3)
        except Exception:
            pass

    with serve(handler, "127.0.0.1", 0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        transport = ProcessPolicyClient(f"ws://127.0.0.1:{server.socket.getsockname()[1]}", rtc=True)
        worker = PolicyWorker(transport)
        try:
            deadline = time.monotonic() + 5
            while not worker.ready and time.monotonic() < deadline:
                time.sleep(.01)
            assert worker.ready
            assert worker.submit(token, RtcJob(observation(), RtcCommitment(30, 9, 39, np.zeros((9, 14))), payload))
            reply = wait_reply(worker)
            assert "committed" in reply.error
            assert reply.pld["unknown_future_trace"]["array"].tolist() == list(range(5))
        finally:
            worker.close()
            transport.close()
            server.shutdown()
            thread.join(timeout=2)


def test_pld_web_has_five_modes_and_no_old_grasp_marker():
    configured = []
    app = create_app(SimpleNamespace(status={}, configure_pld=lambda **kw: configured.append(kw)))
    with TestClient(app) as client:
        for mode in MODES:
            assert client.post("/pld/settings", json={"mode": mode}, headers={"X-YAM-Control": "1"}).status_code == 200
        assert client.post("/pld/settings", json={"mode": "old_rl"}, headers={"X-YAM-Control": "1"}).status_code == 422
        assert client.post("/parts/grasp", json={"arm": "left"}, headers={"X-YAM-Control": "1"}).status_code == 404
        html = client.get("/").text
        assert "PLD-Grasp" in html and 'value="probe"' in html
        assert "--parts-config" not in html and "入口高度" not in html
        assert 'id="delete-last-episode"' in html
        assert 'class="policy-disclosure rl-diagnostics"' in html
        diagnostics = html.split('class="policy-disclosure rl-diagnostics"', 1)[1].split("</details>", 1)[0]
        for field in ("pld-contract", "pld-snapshot", "rl-control-mode", "rl-observation-panel"):
            assert f'id="{field}"' in diagnostics
        assert html.count('id="delete-last-episode"') == 1  # Reuse deletion and safety checks.
        assert 'required' in html.split('id="pld-correct"', 1)[1].split('/>', 1)[0]
    assert [item["mode"] for item in configured] == list(MODES)


def test_runtime_probe_uses_native_scheduler_and_records_trace(tmp_path):
    class ProbePolicy:
        rtc = True
        metadata = metadata()

        def infer_rtc(self, obs, *, target_start_tick, committed_actions, pld):
            time.sleep(.02)
            base = np.tile(obs["observation.state"], (50, 1))
            base[:len(committed_actions)] = committed_actions
            reply = response(pld, base)
            reply["pld"].update(start_tick=target_start_tick, delay_steps=len(committed_actions),
                                 state=obs["observation.state"].copy())
            return reply

    io = StationIO(build_arm_units(StationConfig(), mock=True), mock=True)
    cameras = [CameraWorker(MockCamera(r, r, width=32, height=32)) for r in ("top", "left", "right")]
    worker = PolicyWorker(ProbePolicy())
    recorder = RecordingSession(tmp_path / "probe", video_backend="libx264")
    runtime = Runtime(io, cameras, worker, recorder, mode="inference",
        settings={"policy_fusion": "rtc", "pld": {"mode": "probe"}, "tick_timeout": 2})
    try:
        for camera in cameras:
            camera.start()
        status = runtime.run(duration=1.3, auto_start=True)
        assert not status.get("error"), status
        assert not runtime.recording_error
    finally:
        worker.close()
        recorder.close()
        for camera in cameras:
            camera.stop()
        io.close()
    rows = list(read_rows(recorder.path / "episode_000001"))
    replies = [r["policy_reply"] for r in rows if (r.get("policy_reply") or {}).get("pld")]
    assert replies
    for reply in replies:
        assert reply["pld"]["episode_id"] == recorder.episode_id
        context = reply["request_context"]
        assert context["previous_command_tick"] == reply["token"]["observation_policy_tick"] - 1
        assert context["previous_command_submitted_at"] <= reply["token"]["observed_at"]
    assert all(r["policy_selection"]["request"] for r in rows if r.get("policy_selection"))
    assert runtime.session.arbiter.replan_period == pytest.approx(.2)  # No forced eight-tick cadence.


def test_gripper_submission_prefix_and_record_share_existing_adapter_margin():
    from yam_abc_reproduce.robot.yam_adapter import YamRobot

    io = StationIO(build_arm_units(StationConfig(), mock=True), mock=True)
    commands = []
    try:
        for unit in io.units:
            robot = YamRobot.__new__(YamRobot)
            robot._n, robot._g_closed, robot._g_open = 6, 0, 1
            robot._gripper_command_margin = .02
            robot._robot = SimpleNamespace(command_joint_pos=lambda q: commands.append(q.copy()),
                get_robot_info=lambda: {"gripper_limits": [6.9, 1.2]})
            unit.robot = robot
        target = np.zeros(14)
        target[13] = 1
        limited = io.limit_policy_target(target)
        submitted = io._write_followers(target)
        np.testing.assert_array_equal(submitted, limited)
        assert commands[0][6] == submitted[6] == .02
        assert commands[1][6] == submitted[13] == .98
        bounds = io.policy_limits()
        assert bounds["lower"][6] == .02 and bounds["upper"][13] == .98
        assert bounds["max_target_step"] is None and bounds["target_slew_limited"] is False
        assert bounds["gripper_motor_limits_rad"] == [[6.9, 1.2], [6.9, 1.2]]
    finally:
        # No real SDK was constructed; avoid calling mock-object stop methods.
        io.units = []
        io.close()


def test_pld_mode_changes_reject_active_episode_and_wrong_control_path():
    runtime = Runtime.__new__(Runtime)
    runtime.session = SimpleNamespace(arbiter=SimpleNamespace(
        phase=SimpleNamespace(value="policy"), intervention_pending=False, rtc_timeline=None))
    runtime.recorder = SimpleNamespace(recording=True, saving=False)
    runtime.maintenance = SimpleNamespace(state="idle", latched=False)
    with pytest.raises(ValueError, match="暂停"):
        runtime.configure_pld(mode="collect")


def test_round_c_n_is_evaluation_only_and_checks_episode_identity():
    from yam_abc_reproduce.hil.core import Phase

    events = []
    runtime = Runtime.__new__(Runtime)
    runtime.session = SimpleNamespace(arbiter=SimpleNamespace(phase=Phase.HOLD))
    runtime.recorder = SimpleNamespace(recording=False, saving=False, episode_id="finished",
        record_policy_reply=lambda row: events.append(row) or True)
    runtime.evaluate_pld_round(correct=7, total=10, episode_id="finished")
    assert events[0]["details"]["evaluation"] == dict(C=7, N=10, purpose="evaluation_only")
    assert "reward" not in events[0]
    with pytest.raises(ValueError):
        runtime.evaluate_pld_round(correct=11, total=10, episode_id="finished")
    with pytest.raises(ValueError):
        runtime.evaluate_pld_round(correct=7, total=10, episode_id="old")


@pytest.mark.parametrize("remote", [False, True])
def test_rotation_clears_finished_episode_identity_but_late_audit_keeps_original(tmp_path, remote):
    recorder = (RemoteRecordingSession if remote else RecordingSession)(
        tmp_path / "old", video_backend="libx264")
    old_session = recorder.path
    try:
        recorder.start_episode()
        old_id = recorder.episode_id
        recorder.stop_episode()
        deadline = time.monotonic() + 5
        while recorder.saving and time.monotonic() < deadline:
            time.sleep(.01)
        recorder.rotate_task(tmp_path / "new", {})
        assert recorder.episode_id is None
        assert recorder.record_policy_reply(dict(event="cancelled", episode_id=old_id), session_path=old_session)
    finally:
        recorder.close()
    assert json.loads((old_session / "policy_events.jsonl").read_text())["episode_id"] == old_id
    assert not (tmp_path / "new" / "policy_events.jsonl").exists()


def test_frontend_capabilities_preserve_draft_and_block_unloaded_actor_modes():
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable")
    source = Path("yam_abc_reproduce/hil/static/app.js").read_text()
    function = source.split("function renderRL(", 1)[1].split("async function poll()", 1)[0]
    script = """
const assert = require('assert/strict'), nodes = new Map();
const $ = id => { if (!nodes.has(id)) nodes.set(id, {value:'', options:[], textContent:''}); return nodes.get(id) };
const text = (id, v) => $(id).textContent = v;
const state = {phase:'hold'}, names = {inference:'模型推理'}, phases = {hold:'已保持'};
let renderedPldMode = null, pldEvaluationEpisode = null;
for (const value of ['off','shadow','probe','collect','eval']) $('pld-mode').options.push({value});
""" + "function renderRL(" + function + """
const props = {connected:true,paused:true,recording:false,policyEditable:true,mode:'inference',
  pld:{mode:'off',supported_modes:['off','probe'],server_phase:'probe_diagnostics_only',actor_loaded:false}};
renderRL(props);
assert.deepEqual($('pld-mode').options.filter(o=>!o.disabled).map(o=>o.value), ['off','probe']);
assert.match($('rl-capability').textContent, /未加载专家/);
$('pld-mode').value = 'probe'; renderRL(props);
assert.equal($('pld-mode').value, 'probe'); // Polling must not erase the unsubmitted draft.
props.pld.mode='probe'; props.pld.replay_contract_ready=false; renderRL(props);
assert.match($('rl-readiness').textContent, /尚未启用专家修正/);
assert.match($('rl-capability').textContent, /不可用于 SAC/);
assert.equal($('rl-prepare').hidden, true); // No redundant prepare action in inference.
state.episode_id='episode-one'; renderRL(props);
$('pld-correct').value='5'; $('pld-total').value='10';
renderRL(props); assert.equal($('pld-correct').value, '5');
state.episode_id='episode-two'; renderRL(props);
assert.equal($('pld-correct').value, ''); assert.equal($('pld-total').value, '');
"""
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)


def test_cli_accepts_new_config_and_rejects_legacy_without_hardware(tmp_path, capsys):
    from yam_abc_reproduce.hil.core import Phase
    from yam_abc_reproduce.hil.run import main

    config = tmp_path / "pld.json"
    config.write_text(json.dumps({"mode": "probe"}))
    main(["--mock", "--mode", "inference", "--policy-fusion", "rtc", "--check", "--pld-config", str(config)])
    assert json.loads(capsys.readouterr().out)["pld_mode"] == "probe"
    with pytest.raises(SystemExit):
        main(["--mock", "--check", "--parts-config", str(config)])
    runtime = Runtime.__new__(Runtime)
    runtime.session = SimpleNamespace(arbiter=SimpleNamespace(
        phase=Phase.HOLD, intervention_pending=False, rtc_timeline=None))
    runtime.recorder = SimpleNamespace(recording=False, saving=False)
    runtime.maintenance = SimpleNamespace(state="idle", latched=False)
    with pytest.raises(ValueError, match="RTC"):
        runtime.configure_pld(mode="probe")


def test_shutdown_after_hold_retains_inflight_reply_without_installing_actions():
    _, token, payload = request()
    raw = response(payload)
    reply = Reply(token, raw["actions"], pld=raw["pld"], pld_request=payload,
                  recording_session="original-session")
    events = []
    runtime = Runtime.__new__(Runtime)
    runtime._pld_pending = dict(cancelled=False, recording_session="original-session")
    runtime.worker = SimpleNamespace(client=SimpleNamespace(timeout=.1), poll=lambda: reply)
    runtime.recorder = SimpleNamespace(record_policy_reply=lambda record, **kw: events.append((record, kw)) or True)
    runtime._finish_pld_request()
    assert runtime._pld_pending is None
    event, context = events[0]
    assert event["details"]["policy_reply"]["pld"] is raw["pld"]
    assert event["details"]["policy_reply"]["discarded"] is True
    assert event["details"]["policy_reply"]["cancelled"] is True
    assert context["session_path"] == "original-session"
