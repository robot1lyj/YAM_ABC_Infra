import threading
import time

import numpy as np
import pytest

from yam_abc_reproduce.hil.core import Arbiter, Mode, Phase
from yam_abc_reproduce.hil.planner_process import ProcessActionPlanner, build_plan
from yam_abc_reproduce.hil.policy import PolicyWorker
from yam_abc_reproduce.hil.session import Session


def chunk(value):
    rows = np.zeros((50, 14))
    rows[:, 0] = value
    return rows


@pytest.mark.parametrize("mode", ["tda_smooth", "sync_hold"])
def test_policy_plan_target_reaches_device_arbiter_without_feedback_slew(mode):
    q = np.zeros(14)
    rows = chunk(1.25)
    rows[:, 6] = 1.0
    arbiter = Arbiter(
        Mode.INFERENCE, streaming=True, external_planner=True,
        policy_fusion=mode, action_dt=0.1, max_action_age=3,
    )
    arbiter.start(q)
    token = arbiter.request(1, 1.0, 1.0)
    plan = build_plan(mode, token, rows, None, 1.1, 0.1, 3)
    assert arbiter.accept_plan(token, plan, 1.1)
    decision = arbiter.step(q, q, now=1.1, dt=0.1, leader_ready=True)
    assert decision.source == "policy"
    expected = rows[0].copy()
    expected[13] = 0.05
    np.testing.assert_allclose(decision.action, expected)


@pytest.mark.parametrize("mode", ["tda_smooth", "sync_hold"])
def test_inference_close_trick_only_changes_grippers_below_threshold(mode):
    arbiter = Arbiter(
        Mode.INFERENCE, streaming=True, external_planner=True,
        policy_fusion=mode, action_dt=0.1, max_action_age=3,
    )
    arbiter.start(np.zeros(14))
    token = arbiter.request(1, 1.0, 1.0)
    rows = np.full((50, 14), 0.4)
    rows[:, 6] = [0.0, 0.299, 0.3, 0.301, *([0.4] * 46)]
    rows[:, 13] = 0.8
    original = rows.copy()
    plan = build_plan(mode, token, rows, None, 1.1, 0.1, 3)
    np.testing.assert_array_equal(rows, original)
    np.testing.assert_allclose(plan["actions"][:4, 6], [0.05, 0.05, 0.3, 0.301])
    np.testing.assert_allclose(plan["actions"][:, 13], 0.8)
    np.testing.assert_allclose(plan["actions"][:, :6], 0.4)


def test_planner_process_restart_keeps_device_arbiter_and_discards_old_plan():
    planner = ProcessActionPlanner()
    q = np.zeros(14)
    arbiter = Arbiter(
        Mode.INFERENCE, streaming=True, external_planner=True,
        action_dt=0.1, max_action_age=3,
    )
    try:
        planner.restart()
        first_pid = planner._process.pid
        arbiter.start(q)
        token = arbiter.request(1, 1.0, 1.0)
        plan = planner.plan("raw", token, chunk(0.1), None, 1.1, 0.1, 3)
        assert arbiter.accept_plan(token, plan, 1.1)
        assert arbiter.step(q, q, now=1.1, dt=0.1, leader_ready=True).source == "policy"
        arbiter.hold(q)
        planner.restart()
        assert planner._process.pid != first_pid
        assert arbiter.phase == Phase.HOLD
        assert not arbiter.accept_plan(token, plan, 1.2)
        assert arbiter.step(q, q, now=1.2, dt=0.1).source == "hold"
    finally:
        planner.close()


def test_planner_raw_and_tda_match_existing_buffer_on_overlapping_chunks():
    from yam_abc_reproduce.hil.action_buffer import ActionBuffer
    from yam_abc_reproduce.hil.tda_buffer import TdaActionBuffer

    for mode, reference in (
        ("raw", ActionBuffer(0.1, max_action_age=3)),
        ("tda_smooth", TdaActionBuffer(0.1)),
    ):
        arbiter = Arbiter(Mode.INFERENCE, streaming=True, external_planner=True,
                          policy_fusion=mode, action_dt=0.1, max_action_age=3)
        q = np.zeros(14)
        arbiter.start(q)
        first = arbiter.request(1, 1.0, 1.0)
        assert reference.integrate(first, chunk(0.1), 1.0, 1.12)
        plan = build_plan(mode, first, chunk(0.1), None, 1.12, 0.1, 3)
        assert arbiter.accept_plan(first, plan, 1.12)
        for now in (1.12, 1.22, 1.32):
            actual = arbiter.action_buffer.current(now)
            expected = reference.current(now)
            assert actual is not None and expected is not None
            if mode == "tda_smooth":
                expected[0][[6, 13]] = 0.05
            np.testing.assert_allclose(actual[0], expected[0])
            assert actual[1] == expected[1]
        second = arbiter.request(2, 1.35, 1.35)
        assert second is not None
        previous = arbiter.action_buffer.snapshot()
        assert reference.integrate(second, chunk(0.4), 1.35, 1.48)
        next_plan = build_plan(mode, second, chunk(0.4), previous, 1.48, 0.1, 3)
        assert arbiter.accept_plan(second, next_plan, 1.48)
        for now in (1.52, 1.62, 1.72):
            actual = arbiter.action_buffer.current(now)
            expected = reference.current(now)
            assert actual is not None and expected is not None
            if mode == "tda_smooth":
                expected[0][[6, 13]] = 0.05
            np.testing.assert_allclose(actual[0], expected[0])
            assert actual[1] == expected[1]


def test_slow_network_and_planner_never_block_device_ticks():
    entered = threading.Event()
    release = threading.Event()

    class SlowThor:
        def infer(self, _obs):
            entered.set()
            assert release.wait(2)
            return {"actions": chunk(0.2)}

    class InProcessPlanner:
        def restart(self):
            pass

        def plan(self, *args):
            return build_plan(*args)

        def close(self):
            pass

    worker = PolicyWorker(SlowThor(), planner=InProcessPlanner())
    arbiter = Arbiter(Mode.INFERENCE, streaming=True, external_planner=True,
                      action_dt=0.1, max_request_age=2, max_action_age=3)
    worker.plan_context = lambda: (
        arbiter.action_buffer.fusion, arbiter.action_buffer.snapshot(), 0.1, 3,
    )
    session = Session(arbiter, worker)
    q = np.zeros(14)
    base = time.monotonic()
    try:
        deadline = time.monotonic() + 1
        while not worker.ready and time.monotonic() < deadline:
            time.sleep(0.001)
        assert worker.ready
        session.tick(q, q, now=base, dt=0.03, observation_id=1,
                     observation={"id": 1}, observed_at=base,
                     leader_ready=True, event="start")
        assert entered.wait(1)
        for n in range(10):
            started = time.monotonic()
            decision = session.tick(q, q, now=base + 0.03 + n * 0.03, dt=0.03,
                                    observation_id=n + 2, observation={"id": n + 2},
                                    observed_at=base + 0.03 + n * 0.03, leader_ready=True)
            assert time.monotonic() - started < 0.02
            assert decision.source == "hold"
        release.set()
        deadline = time.monotonic() + 1
        while worker._replies.empty() and time.monotonic() < deadline:
            time.sleep(0.001)
        assert not worker._replies.empty()
        decision = session.tick(q, q, now=base + 0.35, dt=0.03,
                                observation_id=20, observation={"id": 20},
                                observed_at=base + 0.35, leader_ready=True)
        assert decision.source == "policy"
    finally:
        release.set()
        worker.close()


def test_sync_hold_external_plan_executes_all_fifty_then_holds():
    q = np.zeros(14)
    arbiter = Arbiter(Mode.INFERENCE, policy_fusion="sync_hold", streaming=True,
                      external_planner=True, action_dt=1 / 30, max_action_age=1.5)
    arbiter.start(q)
    token = arbiter.request(1, 1.0, 1.0)
    plan = build_plan("sync_hold", token, chunk(0.1), None, 1.17, 1 / 30, 1.5)
    assert arbiter.accept_plan(token, plan, 1.17)
    for step in range(50):
        decision = arbiter.step(q, q, now=1.17 + step / 30,
                                dt=1 / 30, leader_ready=True)
        assert decision.source == "policy" and decision.action_index == step
    assert arbiter.step(q, q, now=1.17 + 50 / 30,
                        dt=1 / 30, leader_ready=True).source == "hold"
    next_token = arbiter.request(2, 1.17 + 50 / 30, 1.17 + 50 / 30)
    assert next_token is not None


def test_old_plan_rejected_after_takeover():
    q = np.zeros(14)
    arbiter = Arbiter(Mode.HIL, streaming=True, external_planner=True,
                      action_dt=0.1, max_action_age=3)
    arbiter.start(q)
    token = arbiter.request(1, 1.0, 1.0)
    plan = build_plan("raw", token, chunk(0.1), None, 1.1, 0.1, 3)
    arbiter.takeover(q, q)
    assert not arbiter.accept_plan(token, plan, 1.1)


def test_planner_loss_holds_without_waiting_for_network():
    class Planner:
        alive = True

        def restart(self):
            self.alive = True

        def close(self):
            pass

    class Thor:
        def infer(self, _obs):
            return {"actions": chunk(0.1)}

    planner = Planner()
    worker = PolicyWorker(Thor(), planner=planner)
    arbiter = Arbiter(Mode.INFERENCE, external_planner=True, streaming=True,
                      action_dt=0.1, max_action_age=3)
    session = Session(arbiter, worker)
    q = np.zeros(14)
    try:
        arbiter.start(q)
        token = arbiter.request(1, 1.0, 1.0)
        plan = build_plan("raw", token, chunk(0.1), None, 1.1, 0.1, 3)
        assert arbiter.accept_plan(token, plan, 1.1)
        assert session.tick(q, q, now=1.2, dt=0.1, observation_id=2,
                            fresh=False, leader_ready=True).phase == Phase.HOLD
        arbiter.start(q)
        token = arbiter.request(3, 2.0, 2.0)
        plan = build_plan("raw", token, chunk(0.1), None, 2.1, 0.1, 3)
        assert arbiter.accept_plan(token, plan, 2.1)
        planner.alive = False
        decision = session.tick(q, q, now=2.2, dt=0.1, observation_id=4,
                                fresh=True, leader_ready=True)
        assert decision.phase == Phase.HOLD and decision.source == "hold"
    finally:
        worker.close()


def test_tda_plan_drops_targets_consumed_while_child_was_planning():
    q = np.zeros(14)
    arbiter = Arbiter(Mode.INFERENCE, streaming=True, external_planner=True,
                      policy_fusion="tda_smooth", action_dt=0.1, max_action_age=3)
    arbiter.start(q)
    first = arbiter.request(1, 1.0, 1.0)
    first_plan = build_plan("tda_smooth", first, chunk(0.1), None, 1.1, 0.1, 3)
    assert arbiter.accept_plan(first, first_plan, 1.1)
    for now in (1.1, 1.2):
        assert arbiter.action_buffer.current(now) is not None
    second = arbiter.request(2, 1.25, 1.25)
    snapshot = arbiter.action_buffer.snapshot()
    new_plan = build_plan("tda_smooth", second, chunk(0.4), snapshot, 1.35, 0.1, 3)
    planned_before = new_plan["actions"].copy()
    # The control loop continues to execute while the child prepares its reply.
    for now in (1.3, 1.4):
        assert arbiter.action_buffer.current(now) is not None
    assert arbiter.accept_plan(second, new_plan, 1.45)
    selected = arbiter.action_buffer.current(1.5)
    np.testing.assert_allclose(selected[0], planned_before[2])
