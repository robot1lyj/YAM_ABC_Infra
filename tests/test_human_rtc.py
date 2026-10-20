"""Human futures never become physical commitments; no motors or Thor model used."""

import copy
import json
import threading
import time
from dataclasses import asdict
from types import SimpleNamespace

import numpy as np
import pytest

from yam_abc_reproduce.hil.core import Arbiter, Mode, Phase, Request
from yam_abc_reproduce.hil.human_rtc import HumanRtc, HumanRtcConfig, build_human_prefix
from yam_abc_reproduce.hil.policy import PolicyWorker, Reply
from yam_abc_reproduce.hil.run import MockPolicy
from yam_abc_reproduce.hil.session import Session


def limit(action):
    action = np.clip(action, -1, 1)
    action[[6, 13]] = np.clip(action[[6, 13]], .02, .98)
    return action


def test_reference_uses_actual_sample_time_and_decays_without_exceeding_limits():
    config = HumanRtcConfig(max_velocity=.5, max_gripper_velocity=.1)
    latest = np.full(14, .5)
    previous = latest - .1
    actions = build_human_prefix(latest, previous, sample_dt=.1, action_dt=1/30,
                                config=config, limit_target=limit)
    assert actions[1, 0] - actions[0, 0] == pytest.approx(.5 * (1 - (3*.05**2 - 2*.05**3)) / 30)
    assert actions[1, 6] - actions[0, 6] < .1/30
    assert np.all(np.diff(actions[:, 0]) >= 0)
    np.testing.assert_array_equal(actions[20:], np.tile(actions[19], (30, 1)))
    clipped = build_human_prefix(np.full(14, .99), previous, sample_dt=.01, action_dt=1/30,
                                config=config, limit_target=limit)
    assert np.max(clipped) <= 1 and np.max(clipped[:, [6, 13]]) <= .98
    stopped = build_human_prefix(latest, latest, sample_dt=.1, action_dt=1/30,
                                config=config, limit_target=limit)
    np.testing.assert_array_equal(stopped, np.tile(latest, (50, 1)))


@pytest.mark.parametrize("settings", [{"enable_human_rtc": 1}, {"enable_human_rtc": "false"},
    {"human_rtc_velocity_decay_steps": 0}, {"human_rtc_prefix_attention_horizon": 50},
    {"human_rtc_max_velocity": float("nan")}, {"human_rtc_max_reference_age": True}])
def test_bad_configuration_is_rejected_before_device_construction(settings):
    with pytest.raises(ValueError):
        HumanRtcConfig.from_settings(settings)


def setup_manual():
    q = np.full(14, .2)
    a = Arbiter(Mode.HIL, policy_fusion="rtc", max_request_age=1, tick_timeout=1)
    a.start(q)
    a.takeover(q, q)
    a.manual_ready(q, q)
    human = HumanRtc(HumanRtcConfig())
    human.epoch = a.epoch
    human.refresh(a, MockPolicy().metadata)
    return a, human, q


def ready_shadow(a, human, q):
    human.record_submitted(a, q, tick=10, now=1)
    human.record_submitted(a, q + .01, tick=11, now=1 + 1/30)
    token = Request(a.epoch, 1, 5, 1 + 1/30, 1, observation_policy_tick=10)
    guidance = human.shadow_request(a, token, tick=11, now=1 + 1/30, limit_target=limit)
    human.pending, human.last_request_at = token, token.created_at
    actions = np.tile(np.arange(50)[:, None] / 100, (1, 14))
    return token, guidance, Reply(token, actions, human_rtc_request=guidance)


@pytest.mark.parametrize("metadata", [{}, {"human_rtc": {"schema": "yam_human_rtc_v1",
    "modes": ["shadow", "handover"], "max_prefix_attention_horizon": 10, "max_velocity_decay_steps": 10}}])
def test_transport_refuses_unsupported_guidance_before_sending(metadata):
    from yam_abc_reproduce.hil.rtc_protocol import RtcPolicyClient
    a, human, q = setup_manual()
    _, guidance, _ = ready_shadow(a, human, q)
    client = object.__new__(RtcPolicyClient)
    client.metadata = metadata
    with pytest.raises(ValueError):
        client.infer_human_rtc({}, human_rtc=guidance)


def test_shadow_reply_is_cached_but_cannot_own_manual_commands_or_rtc_queue():
    a, human, q = setup_manual()
    token, _, reply = ready_shadow(a, human, q)
    worker = SimpleNamespace(poll=lambda: reply, planner_alive=True, ready=True)
    session = Session(a, worker, human_rtc=human)
    decision = session.tick(q, q, now=1.1, dt=1/30, observation_id=5, policy_tick=13)
    assert decision.source == "human" and not decision.policy_valid
    np.testing.assert_array_equal(decision.action, q)
    assert a.pending is None and a.rtc_timeline.remaining(13) == 0
    assert human.cached[0] == token and human.accepted == 1
    assert session.last_reply["execution_eligible"] is False


def test_background_transport_failure_holds_even_after_capability_refresh_clears_pending():
    a, human, q = setup_manual()
    _, _, reply = ready_shadow(a, human, q)
    reply.error, reply.actions = "connection failed", None
    # PolicyWorker clears readiness before the control owner polls the error.
    human.refresh(a, MockPolicy().metadata, ready=False)
    worker = SimpleNamespace(poll=lambda: reply, planner_alive=True, ready=False,
                             mark_failed=lambda *args, **kwargs: None)
    s = Session(a, worker, human_rtc=human)
    d = s.tick(q, q, now=1.1, dt=1/30, observation_id=5, policy_tick=13)
    assert d.source == "hold" and a.phase == Phase.HOLD
    assert s.policy_error == "connection failed"
    assert human.cached is None and not d.policy_valid


@pytest.mark.parametrize("event", ["hold", "stop", "mode:teleop", "end_intervention:collect"])
def test_pause_stop_and_mode_change_reject_inflight_shadow(event):
    a, human, q = setup_manual()
    _, _, reply = ready_shadow(a, human, q)
    worker = SimpleNamespace(poll=lambda: reply, planner_alive=True, ready=True)
    session = Session(a, worker, human_rtc=human)
    decision = session.tick(q, q, now=1.1, dt=1/30, observation_id=5, policy_tick=13, event=event)
    assert decision.source == "hold" and human.cached is None and human.pending is None
    assert session.last_reply["discarded"]
    assert a.rtc_timeline.remaining(13) == 0


def test_explicit_handover_rebases_cache_and_leaves_committed_hold_untouched():
    a, human, q = setup_manual()
    source, _, reply = ready_shadow(a, human, q)
    assert human.accept(a, reply, now=1.1)
    session = Session(a, human_rtc=human)
    decision = session.tick(q, q, now=1.12, dt=1/30, observation_id=6,
                            policy_tick=14, event="resume_policy")
    assert decision.source == "hold" and a.phase == Phase.RESUME
    token = Request(a.epoch, 2, 6, 1.12, 1.1, observation_policy_tick=13, rtc_delay_steps=9)
    guidance = human.handover_request(token, now=1.12, limit_target=limit)
    assert guidance["source_request"] == asdict(source)
    assert guidance["source_model_index"] == 3
    np.testing.assert_array_equal(guidance["reference_actions"][0], limit(reply.actions[3]))
    # The normal RTC timeline continues to commit only the actual HOLD target.
    a.rtc_timeline.record_submitted(13, q)
    commitment = a.rtc_timeline.prepare(observation_tick=13, current_tick=13, limit_target=limit)
    np.testing.assert_allclose(commitment.actions, np.tile(q, (9, 1)))
    assert human.handover_request(token, now=2, limit_target=limit) is None


def test_held_handback_keeps_background_but_generic_pause_cancels_it():
    a, human, q = setup_manual()
    s = Session(a, human_rtc=human)
    s.tick(q, q, now=1, dt=1/30, observation_id=1, policy_tick=1, event="handback_hold")
    assert a.phase == Phase.HOLD and human.background_allowed(a)
    s.tick(q, q, now=1.1, dt=1/30, observation_id=2, policy_tick=2, event="hold")
    assert not human.background_allowed(a)


@pytest.mark.parametrize("change", ["late", "epoch", "sample_gap"])
def test_expired_scope_and_missing_submissions_never_seed_handover(change):
    a, human, q = setup_manual()
    _, _, reply = ready_shadow(a, human, q)
    if change == "late":
        assert not human.accept(a, reply, now=2)
    elif change == "epoch":
        assert human.accept(a, reply, now=1.1)
        a.hold(q)  # e.g. task/source/recovery changed outside Session.tick.
        assert human.transition(a, "resume_policy", 1.2) is None
    else:
        human.pending = None
        human.record_submitted(a, q, tick=20, now=1.2)
        new = Request(a.epoch, 2, 6, 1.2, 1.2, observation_policy_tick=20)
        assert human.shadow_request(a, new, tick=20, now=1.2, limit_target=limit) is None


@pytest.mark.parametrize("change", ["unsupported", "keyboard", "leaders", "pld", "disabled"])
def test_unsupported_context_clears_cache_without_changing_manual_control(change):
    a, human, q = setup_manual()
    _, _, reply = ready_shadow(a, human, q)
    assert human.accept(a, reply, now=1.1)
    if change == "keyboard":
        a.hil_input = "keyboard"
    if change == "disabled":
        human.config = HumanRtcConfig(enabled=False)
    human.refresh(a, {} if change == "unsupported" else MockPolicy().metadata,
                  leaders_connected=change != "leaders", pld=change == "pld")
    assert not human.active and human.cached is None
    assert a.phase == Phase.HUMAN


@pytest.mark.parametrize("bad", [None, "identity", "confirmation", "prefix"])
def test_process_transport_negotiates_shadow_then_strict_handover(bad):
    from openpi_client import msgpack_numpy
    from websockets.sync.server import serve

    from yam_abc_reproduce.hil.policy_process import ProcessPolicyClient

    a, human, q = setup_manual()
    token, guidance, reply = ready_shadow(a, human, q)
    requests = []

    def handler(ws):
        packer = msgpack_numpy.Packer()
        meta = dict(MockPolicy().metadata, rtc_mode="trained", rtc_max_delay_steps=10,
                    action_horizon=50, action_dim=14, action_dt_s=1/30)
        ws.send(packer.pack(meta))
        for _ in range(2):
            request = msgpack_numpy.unpackb(ws.recv(timeout=4))
            requests.append(request)
            actions = request["human_rtc"]["reference_actions"].copy()
            if "rtc" in request:
                prefix = request["rtc"]["committed_actions"]
                actions[:len(prefix)] = prefix
                if bad == "prefix":
                    actions[0, 0] += .1
            confirmation = {k: v for k, v in request["human_rtc"].items() if k != "reference_actions"}
            if len(requests) == 2 and bad == "identity":
                confirmation["epoch"] += 1
            response = dict(actions=actions, human_rtc=confirmation,
                server_timing={"rtc_used": "rtc" in request,
                               "human_rtc_used": not (len(requests) == 2 and bad == "confirmation")})
            ws.send(packer.pack(response))

    with serve(handler, "127.0.0.1", 0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = ProcessPolicyClient(f"ws://127.0.0.1:{server.socket.getsockname()[1]}", rtc=True, timeout=2)
        try:
            result = client.infer_human_rtc({"observation.state": q, "prompt": "pick",
                **{f"observation.images.{r}_rgb": np.zeros((8, 8, 3), np.uint8) for r in ("top", "left", "right")}},
                human_rtc=guidance)
            reply.actions = result["actions"]
            assert human.accept(a, reply, now=1.1)
            candidate = human.transition(a, "resume_policy", 1.12)
            a.resume_policy(q)
            human.after_transition(a, "resume_policy", candidate)
            new = Request(a.epoch, 2, 6, 1.12, 1.1, observation_policy_tick=13, rtc_delay_steps=9)
            handover = human.handover_request(new, now=1.12, limit_target=limit)
            obs = {"observation.state": q, "prompt": "pick",
                   **{f"observation.images.{r}_rgb": np.zeros((8, 8, 3), np.uint8) for r in ("top", "left", "right")}}
            if bad is None:
                result = client.infer_rtc(obs, target_start_tick=13, committed_actions=np.tile(q, (9, 1)),
                                          human_rtc=handover)
                np.testing.assert_allclose(result["actions"][:9], np.tile(q, (9, 1)))
            else:
                with pytest.raises(ValueError, match="Human RTC|committed"):
                    client.infer_rtc(obs, target_start_tick=13, committed_actions=np.tile(q, (9, 1)),
                                     human_rtc=handover)
            assert "rtc" not in requests[0]
            assert requests[1]["human_rtc"]["reference_source"] == "shadow_model"
        finally:
            client.close()
            server.shutdown()
            thread.join(timeout=2)


def test_runtime_records_background_and_handover_without_executing_shadow(tmp_path):
    from yam_abc_reproduce.camera.mock_camera import MockCamera
    from yam_abc_reproduce.camera.worker import CameraWorker
    from yam_abc_reproduce.config import StationConfig
    from yam_abc_reproduce.hil.recording import RecordingSession
    from yam_abc_reproduce.hil.run import Runtime
    from yam_abc_reproduce.hil.station import StationIO
    from yam_abc_reproduce.runtime import build_arm_units

    class TrackingPolicy(MockPolicy):
        def __init__(self):
            super().__init__(rtc=True)
            self.requests = []

        def infer_human_rtc(self, obs, *, human_rtc):
            self.requests.append(copy.deepcopy(human_rtc))
            return super().infer_human_rtc(obs, human_rtc=human_rtc)

        def infer_rtc(self, obs, **kwargs):
            if kwargs.get("human_rtc"):
                self.requests.append(copy.deepcopy(kwargs["human_rtc"]))
            return super().infer_rtc(obs, **kwargs)

    policy = TrackingPolicy()
    io = StationIO(build_arm_units(StationConfig(), mock=True), mock=True)
    pressed = threading.Event()
    read = io.read

    def with_buttons():
        q, leader, _, ages = read()
        return q, leader, [[False, False], [pressed.is_set(), False]], ages

    io.read = with_buttons
    cameras = [CameraWorker(MockCamera(r, r, width=32, height=32)) for r in ("top", "left", "right")]
    worker = PolicyWorker(policy)
    recorder = RecordingSession(tmp_path / "human", video_backend="libx264")
    runtime = Runtime(io, cameras, worker, recorder, mode="hil",
                      settings={"policy_fusion": "rtc", "tick_timeout": 2, "replan_period": .1})
    failures = []

    def wait_for(predicate):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            if predicate():
                return
            if runtime.status.get("error"):
                raise AssertionError(runtime.status)
            time.sleep(.01)
        raise AssertionError(runtime.status)

    def operator():
        try:
            wait_for(lambda: runtime.status.get("phase") == "policy")
            runtime.event("takeover")
            wait_for(lambda: runtime.status.get("phase") == "takeover")
            pressed.set()
            wait_for(lambda: runtime.status.get("phase") == "human")
            pressed.clear()
            wait_for(lambda: runtime.status.get("human_rtc", {}).get("accepted_replies", 0) >= 2)
            pressed.set()
            wait_for(lambda: runtime.status.get("phase") == "hold")
            pressed.clear()
            wait_for(lambda: runtime.status.get("human_rtc", {}).get("accepted_replies", 0) >= 4)
            runtime.event("resume_policy")
            wait_for(lambda: any(r["mode"] == "handover" for r in policy.requests))
            wait_for(lambda: runtime.status.get("phase") == "policy")
            runtime.event("hold")
        except Exception as exc:
            failures.append(exc)
        finally:
            runtime.stopping.set()

    driver = threading.Thread(target=operator)
    try:
        for camera in cameras:
            camera.start()
        driver.start()
        status = runtime.run(duration=10, auto_start=True)
        driver.join(timeout=1)
        assert not failures, failures
        assert not status.get("error"), status
    finally:
        runtime.stopping.set()
        driver.join(timeout=2)
        worker.close()
        recorder.close()
        for camera in cameras:
            camera.stop()
        io.close()
    events = [json.loads(line) for line in (recorder.path / "policy_events.jsonl").read_text().splitlines()]
    replies = [e["details"]["policy_reply"] for e in events if e["event"] == "reply"]
    shadow = [r for r in replies if r["human_rtc_request"]["mode"] == "shadow"]
    assert shadow and all(r["execution_eligible"] is False for r in shadow)
    for record in shadow:
        samples = record["request_context"]["human_rtc_samples"]
        assert len(samples) == 2 and samples[0]["tick"] + 1 == samples[1]["tick"]
        assert samples[1]["tick"] == record["human_rtc_request"]["source_tick"]
        np.testing.assert_array_equal(samples[1]["submitted_action"],
                                      record["human_rtc_request"]["reference_actions"][0])
    assert any(r["human_rtc_request"]["mode"] == "handover" and not r["discarded"] for r in replies)
