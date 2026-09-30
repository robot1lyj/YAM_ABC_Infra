"""No-SDK acceptance of the PARTS raw contract and RTC edit boundary."""

import json
import queue
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from yam_abc_reproduce.hil.core import Request
from yam_abc_reproduce.hil.parts import PartsClient, PartsConfig
from yam_abc_reproduce.hil.parts.height import Heights
from yam_abc_reproduce.hil.parts.machine import Attempts
from yam_abc_reproduce.hil.parts.mock import (
    MockKinematics,
    mock_config,
    mock_metadata,
    mock_reply,
    produce,
)
from yam_abc_reproduce.hil.parts.outbox import DirectoryTransport, Outbox
from yam_abc_reproduce.hil.parts.protocol import handshake, validate_reply
from yam_abc_reproduce.hil.parts.recording import PartsJournal, jsonl, validate_package
from yam_abc_reproduce.hil.parts.reward import components
from yam_abc_reproduce.hil.policy import PolicyJob, PolicyWorker, Reply, RtcJob
from yam_abc_reproduce.hil.rtc_timeline import RtcTimeline


def forces(t, effort=0.8, *, stamp=None):
    return {
        a: dict(
            valid=True,
            effort_nm=effort,
            sampled_at=t,
            sdk_updated_at=1000 + t if stamp is None else stamp,
            feedback_age_s=0.005,
        )
        for a in ("left", "right")
    }


def height(z):
    return {a: dict(height_m=z, height_valid=True, error_m=z - 0.02) for a in ("left", "right")}


def machine():
    logs = []
    state = Attempts(mock_config(), lambda k, v: logs.append((k, v)))
    for a in ("left", "right"):
        state.mark(a, eligible=True, empty_hand=True)
    return state, logs


def observe(state, t, z, **kwargs):
    state.observe(
        tick=round(t * 30),
        now=t,
        epoch=kwargs.pop("epoch", 1),
        heights=height(z),
        feedback=kwargs.pop("feedback", forces(t)),
        policy_active=kwargs.pop("policy_active", True),
    )


def close(state, t=0.08):
    q = np.zeros(14)
    q[[6, 13]] = 0.8
    state.submitted(tick=1, now=0.01, target=q, selection=None, residual=np.zeros(14))
    q[[6, 13]] = 0.1
    state.submitted(tick=2, now=t, target=q, selection=None, residual=np.zeros(14))


def test_configuration_off_defaults_unset_goal_and_no_invented_rewards():
    cfg = PartsConfig.from_dict()
    assert cfg.mode == "off" and cfg.left.h_entry_m == cfg.right.h_entry_m == 0.05
    assert cfg.left.h_goal_m is None
    assert components(cfg, error_m=None)["total_reward"] is None
    with pytest.raises(ValueError, match="unset"):
        PartsConfig.from_dict({"mode": "collect"}).validate_execution()


def test_height_uses_calibration_not_base_z_and_unknown_goal_null():
    data = mock_config().as_dict()
    data["left"]["table"]["point"][2] = 0.1
    data["right"]["table"] = None
    data["left"]["h_goal_m"] = None
    h = Heights(PartsConfig.from_dict(data), MockKinematics())
    q = np.zeros(14)
    q[[0, 7]] = 0.15
    result = h.sample(q, sampled_at=1, feedback_age_s=[0, 0], now=1)
    assert result["left"]["height_m"] == pytest.approx(0.05)
    assert result["left"]["error_m"] is None
    assert result["right"]["height_m"] is None
    assert not h.sample(q, sampled_at=1, feedback_age_s=[0, 0], now=2)["left"]["height_valid"]


def test_50mm_crossing_competition_empty_hand_and_start_below():
    state, logs = machine()
    observe(state, 0, 0.06)
    observe(state, 0.04, 0.05)
    assert state.active_arm == "left" and state.phase["right"] == "READY"
    assert next(v for _, v in logs if v["kind"] == "entry")["competing_arms"] == ["left", "right"]
    missed, logs = machine()
    observe(missed, 0, 0.04)
    assert missed.attempt is None and missed.phase["left"] == "WAIT_REARM"
    assert any(v["kind"] == "entry_missed" for _, v in logs)
    blocked, _ = machine()
    blocked.mark("left", eligible=True, empty_hand=False)
    blocked.mark("right", eligible=False, empty_hand=True)
    observe(blocked, 0, 0.06)
    observe(blocked, 0.04, 0.05)
    assert blocked.attempt is None


def test_signed_strict_force_only_after_close_advancing_and_once_at_handback():
    state, logs = machine()
    observe(state, 0, 0.06)
    observe(state, 0.04, 0.04)
    for t in (0.08, 0.12, 0.16, 0.20):
        observe(state, t, 0.04, feedback=forces(t, -1))
    assert state.phase["left"] == "ACTIVE_DESCENT"
    close(state, 0.21)
    for t in (0.24, 0.28, 0.32, 0.36):
        observe(state, t, 0.04, feedback=forces(t, -0.65))
    assert state.phase["left"] == "ACTIVE_CLOSURE"
    for t in (0.40, 0.44, 0.48, 0.52):
        observe(state, t, 0.04, feedback=forces(t, -0.8))
    assert state.phase["left"] == "EXIT_PENDING"
    assert state.handback(tick=16, now=0.52)
    assert not state.handback(tick=17, now=0.53)
    attempts = [v for k, v in logs if k == "attempt"]
    assert len(attempts) == 1 and attempts[0]["grasp_reward"] == 1
    assert attempts[0]["closure_tick"] == 2


def test_duplicate_old_and_nonfinite_force_dont_confirm():
    state, _ = machine()
    observe(state, 0, 0.06)
    observe(state, 0.04, 0.04)
    close(state)
    for t in (0.1, 0.14, 0.18, 0.22):
        observe(state, t, 0.04, feedback=forces(t, stamp=1000.1))
    assert state.phase["left"] == "ACTIVE_CLOSURE"
    bad = forces(0.24)
    bad["left"]["feedback_age_s"] = 0.2
    observe(state, 0.24, 0.04, feedback=bad)
    assert state.completed[-1]["result"] == "canceled"
    assert state.completed[-1]["grasp_reward"] is None
    invalid, _ = machine()
    observe(invalid, 0, 0.06)
    observe(invalid, 0.04, 0.04)
    close(invalid)
    observe(invalid, 0.12, 0.04, feedback=forces(0.12, float("nan")))
    assert invalid.completed[-1]["result"] == "canceled"


def test_failure_cancel_epoch_and_rearm_are_separate():
    state, _ = machine()
    observe(state, 0, 0.06)
    observe(state, 0.04, 0.04)
    close(state)
    q = np.zeros(14)
    q[[6, 13]] = 0.8
    state.submitted(tick=3, now=0.12, target=q, selection=None, residual=np.zeros(14))
    assert state.attempt["pending_result"] == "failure"
    assert state.handback(tick=4, now=0.13)
    assert state.completed[-1]["grasp_reward"] == 0
    state.submitted(tick=5, now=0.16, target=q, selection=None, residual=np.zeros(14))
    assert state.phase["left"] == "WAIT_REARM"
    observe(state, 0.2, 0.06)
    state.submitted(tick=6, now=0.2, target=q, selection=None, residual=np.zeros(14))
    assert state.phase["left"] == "READY"
    observe(state, 0.21, 0.06)
    observe(state, 0.24, 0.04)
    observe(state, 0.28, 0.04, epoch=2)
    assert state.completed[-1]["result"] == "canceled"


def context(config):
    return dict(
        protocol="yam-parts-v1",
        contract_sha=config.contract_sha,
        mode=config.mode,
        context=dict(
            run_id="r",
            session_id="s",
            epoch=1,
            request_id=1,
            observation_id=2,
            observation_policy_tick=10,
        ),
    )


@pytest.mark.parametrize(
    "corruption", ["shape", "nan", "epoch", "contract", "mask", "exploration", "indices"]
)
def test_protocol_rejects_bad_contracts(corruption):
    cfg = mock_config("eval")
    metadata = mock_metadata(cfg)
    request = context(cfg)
    value = mock_reply(request, None, cfg)
    if corruption == "shape":
        value["candidates"]["left"]["u"] = np.zeros((50, 1))
    if corruption == "nan":
        value["candidates"]["left"]["u"][20, 0] = np.nan
    if corruption == "epoch":
        value["context"] = dict(value["context"], epoch=2)
    if corruption == "contract":
        value["contract_sha"] = "other"
    if corruption == "mask":
        value["candidates"]["left"]["editable_mask"][0] = True
    if corruption == "exploration":
        value["candidates"]["left"]["exploration_applied"] = True
    if corruption == "indices":
        metadata["parts"]["per_arm"]["left"]["indices"][0] = 6
    with pytest.raises(ValueError):
        validate_reply(value, request, handshake(metadata, cfg), cfg, delay_steps=9)


def test_worker_preserves_plain_and_rtc_parts_without_second_inflight():
    cfg = mock_config()
    payload = context(cfg)
    client = SimpleNamespace(
        infer=lambda obs: {"actions": np.zeros((50, 14)), "parts": obs["parts"]},
        infer_rtc=lambda obs, **kw: {"actions": np.zeros((50, 14)), "parts": kw["parts"]},
    )
    worker = PolicyWorker(client)
    token = Request(1, 1, 2, 0, 0)
    try:
        for job in (
            PolicyJob({}, payload),
            RtcJob({}, SimpleNamespace(observation_tick=0, actions=np.zeros((9, 14))), payload),
        ):
            assert worker.submit(token, job)
            assert not worker.submit(token, job)
            deadline = time.monotonic() + 2
            reply = None
            while reply is None and time.monotonic() < deadline:
                reply = worker.poll()
                time.sleep(0.005)
            assert reply.parts == payload
    finally:
        worker.close()


def test_shadow_unsupported_keeps_baseline_exact_and_records_raw():
    cfg = mock_config()
    logs = []
    client = PartsClient(
        cfg,
        run_id="r",
        session_id="s",
        emit=lambda k, v: logs.append((k, v)),
        kinematics=MockKinematics(),
    )
    client.observe(
        tick=10,
        now=1,
        epoch=1,
        state=np.zeros(14),
        ages=[0, 0],
        feedback=[None, None],
        policy_active=True,
    )
    token = Request(1, 1, 2, 1, 1)
    client.build_request(token, observation_tick=10)
    client.accept_reply(Reply(token, np.zeros((50, 14))), metadata={})
    assert client.capability == "unsupported"
    base = np.arange(14) / 100
    edited, owner = client.edit(
        10, base, {}, limit_target=lambda _: pytest.fail("shadow changed limits path")
    )
    np.testing.assert_array_equal(edited, base)
    assert owner["parts"]["physical_residual_rad"] == [0] * 14
    assert logs[-1][1]["actions_native"].shape == (50, 14)


def test_rtc_committed_final_targets_and_old_owner_survive_new_reply():
    timeline = RtcTimeline(delay_steps=3)
    timeline.edit_target = lambda tick, base, owner: (
        base + np.r_[0.01, np.zeros(13)],
        dict(owner or {}, edited=True),
    )
    timeline._plan = {i: np.zeros(14) for i in range(1, 30)}
    timeline._owners = {i: dict(request={"request_id": 1}, target_tick=i) for i in range(1, 30)}
    timeline.record_submitted(0, np.zeros(14))
    commitment = timeline.prepare(observation_tick=0, current_tick=0, limit_target=lambda x: x)
    np.testing.assert_allclose(commitment.actions[1:, 0], 0.01)
    replies = np.zeros((50, 14))
    replies[:3] = commitment.actions
    assert timeline.install(
        commitment, replies, current_tick=1, limit_target=lambda x: x, request={"request_id": 2}
    )
    target, _ = timeline.select(1, np.zeros(14))
    assert target[0] == 0.01 and timeline.last_selection["request"]["request_id"] == 1
    assert timeline.prefix_sources(0, 3)[1]["selection"]["request"]["request_id"] == 1
    timeline.record_submitted(1, target)
    assert timeline.prefix_sources(0, 3)[1]["selection"]["edited"]


def test_future_handback_compares_adjacent_final_target_not_live_command():
    cfg = mock_config("eval")
    client = PartsClient(
        cfg,
        run_id="r",
        session_id="s",
        emit=lambda k, v: None,
        kinematics=MockKinematics(),
    )
    client.epoch = 1
    client.machine, _ = machine()
    observe(client.machine, 0, 0.06)
    observe(client.machine, 0.04, 0.04)
    client.machine.phase["left"] = "EXIT_PENDING"
    client.machine.attempt["exit_requested_tick"] = 2
    client.machine.previous_command = {"target": np.zeros(14)}
    candidate = mock_reply(context(cfg), None, cfg)
    client.replies[(1, 1)] = candidate
    timeline = RtcTimeline(delay_steps=9)
    timeline.record_submitted(0, np.zeros(14))
    previous = np.zeros(14)
    previous[0] = 0.35
    timeline._committed[8] = (previous.copy(), "policy")
    timeline._plan[9] = np.ones(14)  # Raw predictions must not masquerade as final targets.
    assert timeline.final_target_at(9) is None
    copied = timeline.final_target_at(8)
    copied[0] = 99
    assert timeline.final_target_at(8)[0] == 0.35
    np.testing.assert_array_equal(timeline.final_target_at(0), np.zeros(14))
    client.preceding_target = timeline.final_target_at
    owner = dict(request=dict(epoch=1, request_id=1), model_index=9)
    base = previous.copy()
    base[0] += 0.01
    target, selected = client.edit(9, base, owner, limit_target=lambda x: x)
    np.testing.assert_array_equal(target, base)
    assert "handback_to_new_base" in selected["parts"]["constraints"]
    bad = base.copy()
    bad[0] += 0.3
    with pytest.raises(ValueError, match="handback discontinuity"):
        client.edit(10, bad, owner, limit_target=lambda x: x)


def test_raw_package_retries_idempotent_and_detects_modified_source(tmp_path):
    root = tmp_path / "parts_mock"
    publication = produce(root, producer_sha="fixture")
    assert publication["client_complete"] and publication["mock"]
    assert not validate_package(root)
    items = jsonl(root / "attempts.jsonl")
    assert any(a["result"] == "success" for a in items)
    assert any(a["result"] == "failure" for a in items)
    assert any(a["result"] == "canceled" for a in items)
    assert all(
        row["parts"]["physical_residual_rad"] == [0] * 14
        for row in __import__("yam_abc_reproduce.hil.storage", fromlist=["read_rows"]).read_rows(
            root / "episodes" / "episode_000001"
        )
        if row.get("parts")
    )
    destination = DirectoryTransport(tmp_path / "receiver")

    class Flaky:
        def __init__(self):
            self.count = 0

        def send(self, *args):
            self.count += 1
            if self.count == 1:
                raise OSError("mock upload down")
            return destination.send(*args)

    outbox = Outbox(tmp_path / "outbox", Flaky())
    pid = outbox.enqueue(root)
    outbox.drain_once()
    assert json.loads((outbox.path / (pid + ".json")).read_text())["state"] == "retry"
    # Reconstruct persistent owner; retry does not depend on process memory.
    outbox = Outbox(outbox.path, destination)
    outbox.drain_once()
    assert json.loads((outbox.path / (pid + ".json")).read_text())["state"] == "acked"
    assert outbox.enqueue(root) == pid
    with (root / "events.jsonl").open("a") as stream:
        stream.write("{}\n")
    assert any("hash_mismatch" in e for e in validate_package(root))


def test_sidecar_queue_overflow_is_visible_and_does_not_wait(tmp_path):
    journal = PartsJournal(
        tmp_path / "raw", {"run_id": "r", "mode": "shadow", "mock": True}, capacity=1
    )
    journal.close()
    assert not journal.submit("event", {})


def test_gradual_closure_and_descent_only_reward_events():
    state, logs = machine()
    observe(state, 0, 0.06)
    observe(state, 0.04, 0.04)
    q = np.zeros(14)
    q[6] = 0.8
    state.submitted(tick=1, now=0.04, target=q, selection=None, residual=np.zeros(14))
    for i in range(1, 7):
        observe(state, (i + 1) / 30, 0.035)
        q[6] -= 0.005
        state.submitted(
            tick=i + 1, now=(i + 1) / 30, target=q, selection=None, residual=np.zeros(14)
        )
    assert state.phase["left"] == "ACTIVE_CLOSURE"
    observe(state, 0.3, 0.035)
    assert state.reward["height_reward"] is None
    for t in (0.34, 0.38, 0.42, 0.46):
        observe(state, t, 0.035)
    assert state.handback(tick=14, now=0.46)
    assert state.reward["grasp_reward"] == 1 and state.reward["valid"]
    assert state.completed[-1]["reward_valid"]
    assert state.event_refs and any(v["kind"] == "success" for k, v in logs if k == "event")
    observe(state, 0.5, 0.035)
    assert state.reward["grasp_reward"] is None  # No repeated success bonus.


@pytest.mark.parametrize("mode", ["collect", "eval"])
def test_full_residual_packages_keep_inactive_arm_and_grippers(tmp_path, mode):
    from yam_abc_reproduce.hil.storage import read_rows

    root = tmp_path / mode
    assert produce(root, producer_sha="fixture", mode=mode)["client_complete"]
    edited = []
    for row in read_rows(root / "episodes" / "episode_000001"):
        parts = row.get("parts") or {}
        delta = np.asarray(parts.get("physical_residual_rad", [0] * 14))
        if np.any(delta):
            assert delta[6] == delta[13] == 0
            other = slice(7, 13) if parts["active_arm"] == "left" else slice(0, 6)
            assert not np.any(delta[other])
            edited.append(row)
    assert edited
    assert not validate_package(root)


def test_bounded_writer_overflow_is_persisted_without_control_wait(tmp_path, monkeypatch):
    gate = threading.Event()
    original = PartsJournal._run

    def blocked(journal):
        gate.wait(2)
        original(journal)

    monkeypatch.setattr(PartsJournal, "_run", blocked)
    journal = PartsJournal(
        tmp_path / "raw", {"run_id": "r", "mode": "shadow", "mock": True}, capacity=1
    )
    try:
        assert journal.submit("event", {"kind": "test"})
        started = time.monotonic()
        assert not journal.submit("event", {"tick": 2})
        assert time.monotonic() - started < 0.1
    finally:
        gate.set()
        journal.close()
    health = json.loads((journal.path / "recording_status.json").read_text())
    assert health["closed"] and health["error"] and health["gaps"][0]["tick"] == 2


def test_scheduler_snapshot_only_final_targets_and_unanswered_request(tmp_path):
    timeline = RtcTimeline(delay_steps=9)
    timeline._plan = {tick: np.ones(14) for tick in range(50)}
    timeline.record_submitted(0, np.full(14, 0.1))
    snap = timeline.scheduler_snapshot(0)
    assert snap["valid_mask"][0] and snap["committed_mask"][0]
    assert not np.any(snap["valid_mask"][1:])
    assert not np.any(snap["targets"][1:])
    logs = []
    client = PartsClient(
        mock_config(),
        run_id="r",
        session_id="s",
        emit=lambda k, v: logs.append((k, v)),
        kinematics=MockKinematics(),
    )
    token = Request(1, 1, 2, 0, 0)
    client.build_request(token, observation_tick=0)
    client.cancel_pending("reset")
    client.accept_reply(Reply(token, np.zeros((50, 14))), metadata={})
    assert len([v for k, v in logs if k == "request"]) == 1
    assert logs[-1][1]["kind"] == "late_or_duplicate_reply"


def test_data_run_replacement_swaps_on_hold_without_io_and_rejects_late_install(tmp_path):
    from yam_abc_reproduce.hil.parts.lifecycle import RunReplacement

    cfg = mock_config()

    def client(name):
        return PartsClient(
            cfg, run_id=name, session_id=name, emit=lambda k, v: None, kinematics=MockKinematics()
        )

    before, after = client("old"), client("new")
    journal = SimpleNamespace(path=tmp_path / "new")
    runtime = SimpleNamespace(
        parts=before,
        parts_journal=None,
        task_switching=True,
        session=SimpleNamespace(
            parts=before, arbiter=SimpleNamespace(phase=SimpleNamespace(value="hold"))
        ),
        recorder=SimpleNamespace(metadata={}),
        task_releases=queue.SimpleQueue(),
    )
    exchange = RunReplacement(before, after, journal)
    exchange.apply(runtime)
    assert exchange.accepted and runtime.session.parts is after
    assert runtime.recorder.metadata["parts"]["run_id"] == "new"
    late = RunReplacement(after, before, journal)
    late.canceled = True
    late.apply(runtime)
    assert not late.accepted and runtime.parts is after


def test_rl_page_entry_read_only_and_marker_api_validated():
    from fastapi.testclient import TestClient

    from yam_abc_reproduce.hil.web import create_app

    calls = []
    service = SimpleNamespace(mark_parts_grasp=lambda **kw: calls.append(kw))
    web = TestClient(create_app(service))
    html = web.get("/").text
    assert 'data-view="rl"' in html and 'id="rl-run-panel"' in html
    assert not calls
    headers = {"x-yam-control": "1"}
    assert web.post("/parts/grasp", json={"arm": "unknown"}, headers=headers).status_code == 422
    assert web.post("/parts/grasp", json={"arm": "left"}, headers=headers).status_code == 200
    assert calls == [dict(arm="left", eligible=True, empty_hand=True, reset=False)]


def test_cli_validates_rl_configuration_before_constructing_devices(tmp_path, monkeypatch, capsys):
    from yam_abc_reproduce.hil import run

    cfg = tmp_path / "parts.json"
    cfg.write_text(json.dumps(mock_config("collect").as_dict()))
    monkeypatch.setattr(
        run, "build_arm_units", lambda *a, **kw: pytest.fail("constructed hardware")
    )
    run.main(["--mock", "--check", "--policy-fusion", "rtc", "--parts-config", str(cfg)])
    assert json.loads(capsys.readouterr().out)["parts_mode"] == "collect"
    cfg.write_text('{"mode":"collect"}')
    with pytest.raises(SystemExit):
        run.main(["--mock", "--check", "--parts-config", str(cfg)])


def test_workbench_forwards_run_locked_parts_config_without_sdk(tmp_path, monkeypatch):
    from yam_abc_reproduce import resource_qos
    from yam_abc_reproduce.hil import run
    from yam_abc_reproduce.hil.workbench import Workbench

    received = []
    monkeypatch.setattr(run, "main", lambda argv, **kw: received.append(argv))
    monkeypatch.setattr(resource_qos, "place_on_cpus", lambda *a: None)
    service = Workbench.__new__(Workbench)
    service.args = SimpleNamespace(
        station="unused",
        mock=True,
        url=None,
        baseline=False,
        output=tmp_path,
        parts_config=tmp_path / "parts.json",
    )
    service.initializing = service.taskless_teleop = False
    service.mode = "inference"
    service._session_task = {"id": "task"}
    service._run_session()
    index = received[0].index("--parts-config")
    assert received[0][index + 1] == str(service.args.parts_config)
