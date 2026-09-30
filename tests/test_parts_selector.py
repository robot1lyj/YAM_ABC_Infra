"""Automatic rules through complete grasp/transport/release cycles, no SDK."""

import copy

import numpy as np
import pytest

from yam_abc_reproduce.hil.parts import PartsClient, PartsConfig
from yam_abc_reproduce.hil.parts.mock import MockKinematics, mock_config, mock_metadata
from yam_abc_reproduce.hil.parts.protocol import handshake
from yam_abc_reproduce.hil.rtc_timeline import RtcTimeline


class Cycle:
    def __init__(self, config=None):
        self.logs = []
        self.client = PartsClient(
            config or mock_config(),
            run_id="auto",
            session_id="s",
            emit=lambda k, v: self.logs.append((k, copy.deepcopy(v))),
            kinematics=MockKinematics(),
        )
        self.tick = -1
        self.state = np.zeros(14)
        self.now = 0

    def step(
        self,
        z=0.08,
        *,
        x=0,
        position=0.9,
        effort=0.1,
        right_position=0.1,
        right_z=0.08,
        stamp=None,
        valid=True,
        missing_position=False,
        epoch=1,
        active=True,
        recording=True,
    ):
        self.tick += 1
        self.now = self.tick / 30
        self.state[[0, 1, 6, 7, 13]] = [z, x, position, right_z, right_position]
        feedback = [
            dict(
                position=float(self.state[i]),
                effort_nm=effort,
                sdk_updated_at=self.now + 1000 if stamp is None else stamp,
                sampled_at=self.now,
                feedback_age_s=0.005 if stamp is None else max(0.005, 1000 + self.now - stamp),
                valid=valid,
            )
            for i in (6, 13)
        ]
        if missing_position:
            feedback[0].pop("position")
        self.client.observe(
            tick=self.tick,
            now=self.now,
            epoch=epoch,
            state=self.state,
            ages=[0, 0],
            feedback=feedback,
            policy_active=active,
            recording=recording,
        )
        return self.client.arm_snapshots()["left"]

    def ready(self, **kw):
        for _ in range(12):
            self.step(**kw)
        return self.client.arm_snapshots()["left"]

    def submit(self, position, right_position=0.1):
        target = self.state.copy()
        target[[6, 13]] = [position, right_position]
        self.client.submitted(self.tick, self.now, target, None)

    def enter(self):
        self.ready()
        self.step(0.049)
        assert self.client.machine.active_arm == "left"

    def kinds(self):
        return [v["kind"] for k, v in self.logs if k == "event"]


def test_actual_open_at_point_eight_requires_ten_fresh_samples_and_point_three_seconds():
    h = Cycle()
    h.enter()
    h.submit(0.1)
    h.ready(z=0.04, position=0.2, effort=0.9)
    h.client.machine.handback(tick=h.tick, now=h.now)
    h.submit(0.9)
    start = h.now + 1 / 30
    for _ in range(9):
        snap = h.step(position=0.8)
        assert not snap["actual_open_confirmed"] and not snap["eligible"]
    snap = h.step(position=0.8)
    assert h.now - start == pytest.approx(0.3)
    assert snap["actual_open_confirmed"] and snap["eligible"]
    snap = h.step(position=0.799)
    assert not snap["actual_open_confirmed"] and snap["eligible"]
    for _ in range(9):
        snap = h.step(position=0.8)
        assert not snap["actual_open_confirmed"]
    assert h.step(position=0.8)["actual_open_confirmed"]


def test_duplicate_feedback_cannot_fill_ten_sample_requirement_after_point_three_seconds():
    h = Cycle()
    for i in range(12):
        snap = h.step(position=0.8, stamp=1000 + (i - i % 2) / 30)
    assert h.now > 0.3 and not snap["actual_open_confirmed"]
    assert h.client.selector.lanes["left"]["open_window"].count == 6


@pytest.mark.parametrize("effort", [0.651, -0.8])
def test_high_gripper_effort_does_not_grant_empty_hand_even_when_open(effort):
    h = Cycle()
    snap = h.ready(effort=effort)
    assert not snap["eligible"] and not snap["empty_hand"]
    assert "gripper_effort_high" in snap["reason_codes"]
    h.step(0.049, effort=effort)
    assert h.client.machine.attempt is None


@pytest.mark.parametrize("count", [0, True, 1.5])
def test_open_confirmation_sample_count_is_validated(count):
    data = mock_config().as_dict()
    data["left"]["open_confirm_samples"] = count
    with pytest.raises(ValueError, match="positive integer"):
        PartsConfig.from_dict(data)


def test_grasp_transport_low_place_release_retract_then_grasp_without_markers():
    h = Cycle()
    h.enter()
    h.submit(0.1)
    for _ in range(6):
        snap = h.step(0.04, position=0.2, effort=-0.8)
    assert snap["holding_locked"] and snap["selector_state"] == "HOLDING"
    assert h.client.machine.phase["left"] == "EXIT_PENDING"
    assert h.client.machine.handback(tick=h.tick, now=h.now)
    assert h.client.machine.completed[-1]["grasp_reward"] == 1
    for _ in range(4):
        snap = h.step(0.02, x=0.4, position=0.2, effort=0.1)
        assert snap["holding_locked"] and not snap["eligible"]
    h.submit(0.9)
    for _ in range(5):
        snap = h.step(0.02, x=0.4, position=0.2)
        assert snap["holding_locked"] and "waiting_actual_release" in snap["reason_codes"]
    for _ in range(12):
        snap = h.step(0.02, x=0.4)
    assert not snap["holding_locked"] and snap["selector_state"] == "REARM_WAIT"
    assert h.client.machine.phase["left"] == "WAIT_REARM"
    assert not h.client.machine.attempt
    h.step(0.08, x=0.4)
    h.step(0.049, x=0.4)
    assert h.client.machine.active_arm == "left"
    assert "release_confirmed" in h.kinds() and "post_grasp_loss" in h.kinds()
    assert len(h.client.machine.completed) == 1


def test_slow_hysteresis_crossing_and_jitter_only_one_attempt():
    value = mock_config().as_dict()
    value["left"]["entry_hysteresis_m"] = 0.005
    h = Cycle(PartsConfig.from_dict(value))
    h.ready()
    for z in (0.054, 0.053, 0.051, 0.049, 0.052, 0.049):
        h.step(z)
    assert h.client.machine.active_arm == "left"
    assert h.kinds().count("entry") == 1


def test_failed_grasp_rearms_after_retract_without_open_confirmation_or_region():
    h = Cycle()
    h.enter()
    h.submit(0.1)
    for _ in range(5):
        h.step(0.04, position=0.1, effort=0.65)
    assert not h.client.selector.lanes["left"]["holding"]
    h.submit(0.9)
    assert h.client.machine.phase["left"] == "EXIT_PENDING"
    assert h.client.machine.handback(tick=h.tick, now=h.now)
    assert h.client.machine.completed[-1]["grasp_reward"] == 0
    h.ready(z=0.08, x=0.4, position=0.1)
    assert h.client.machine.phase["left"] == "READY"
    assert not h.client.arm_snapshots()["left"]["actual_open_confirmed"]
    h.step(0.049, x=0.4, position=0.1)
    assert h.client.machine.active_arm == "left"


def test_base_only_closure_before_crossing_locks_holding_without_rl_reward():
    h = Cycle()
    h.ready(x=0.4)
    h.submit(0.1)
    h.ready(z=0.04, x=0.4, position=0.2, effort=0.9)
    snap = h.step(0.02, position=0.2, effort=0.1)
    assert snap["holding_locked"] and not snap["empty_hand"]
    assert not h.client.machine.completed and not h.client.machine.attempt
    assert not h.client.machine.reward["valid"]


@pytest.mark.parametrize(
    "kwargs", [dict(stamp=1000), dict(valid=False), dict(missing_position=True)]
)
def test_repeated_invalid_or_missing_actual_position_never_arms(kwargs):
    h = Cycle()
    h.ready(**kwargs)
    snap = h.step(0.049, **kwargs)
    assert not snap["eligible"] and h.client.machine.attempt is None


@pytest.mark.parametrize("kwargs", [dict(epoch=2), dict(active=False), dict(valid=False)])
def test_session_gaps_cancel_and_new_fresh_height_force_can_rearm_without_open(kwargs):
    h = Cycle()
    h.enter()
    h.submit(0.1)
    h.step(0.04, position=0.1, **kwargs)
    assert h.client.machine.completed[-1]["result"] == "canceled"
    h.ready(z=0.08, position=0.1, epoch=kwargs.get("epoch", 1))
    assert not h.client.machine.attempt
    snap = h.client.arm_snapshots()["left"]
    assert snap["eligible"] and snap["empty_hand"]
    assert not snap["actual_open_confirmed"]


def test_start_closed_high_effort_blocks_but_low_effort_and_retract_rearm():
    h = Cycle()
    h.ready(z=0.02, position=0.1, effort=0.9)
    assert not h.client.machine.attempt
    assert h.client.machine.phase["left"] == "WAIT_REARM"
    assert not h.client.arm_snapshots()["left"]["empty_hand"]
    h.ready(z=0.02)
    assert not h.client.machine.attempt
    h.step(0.08)
    h.step(0.049)
    assert h.client.machine.active_arm == "left"


def test_two_arms_compete_and_left_holding_does_not_block_right():
    h = Cycle()
    h.ready(right_position=0.9)
    h.step(0.049, right_z=0.049, right_position=0.9)
    assert h.client.machine.active_arm == "left"
    entries = [v for k, v in h.logs if k == "event" and v["kind"] == "entry"]
    assert entries[-1]["competing_arms"] == ["left", "right"]
    h.submit(0.1, right_position=0.9)
    h.ready(z=0.04, position=0.2, effort=0.9, right_position=0.9)
    assert h.client.machine.handback(tick=h.tick, now=h.now)
    h.step(0.04, position=0.2, right_position=0.9)
    h.step(0.04, position=0.2, right_z=0.049, right_position=0.9)
    assert h.client.machine.active_arm == "right"
    assert h.client.arm_snapshots()["left"]["holding_locked"]


def test_old_residual_prefix_blocks_rearm_without_rewriting_it():
    h = Cycle()
    timeline = RtcTimeline()
    target = np.zeros(14)
    timeline._committed[100] = (target.copy(), "policy")
    timeline._owners[100] = {"parts": {"physical_residual_rad": [0.001] + [0] * 13}}
    h.client.committed_arms = timeline.pending_residual_arms
    h.ready()
    assert "old_residual_committed" in h.client.arm_snapshots()["left"]["reason_codes"]
    h.step(0.049)
    assert h.client.machine.attempt is None
    np.testing.assert_array_equal(timeline.final_target_at(100), target)
    timeline.clear()
    h.step(0.08)
    h.step(0.049)
    assert h.client.machine.active_arm == "left"


def test_preclosing_does_not_disarm_and_xy_motion_does_not_cancel_attempt():
    h = Cycle()
    h.ready()
    h.submit(0.1)
    h.step(0.049)
    assert h.client.machine.active_arm == "left"
    h = Cycle()
    h.enter()
    h.step(0.04, x=0.4)
    assert h.client.machine.phase["left"] == "ACTIVE_DESCENT"
    assert not h.client.machine.completed


def test_rule_version_and_protocol_hash_are_locked():
    config = mock_config("collect")
    metadata = mock_metadata(config)
    metadata["parts"].pop("selector_config_sha")
    with pytest.raises(ValueError, match="handshake"):
        handshake(metadata, config)
    data = config.as_dict()
    data["left"]["open_confirm_s"] += 0.1
    changed = PartsConfig.from_dict(data)
    assert changed.selector_config_sha != config.selector_config_sha
    with pytest.raises(ValueError, match="handshake"):
        handshake(mock_metadata(config), changed)
    metadata = mock_metadata(config)
    metadata["parts"]["selector_schema"] = "rules_auto_v1"
    with pytest.raises(ValueError, match="handshake"):
        handshake(metadata, config)


def test_rule_configuration_owns_nested_inputs_after_hashing():
    data = mock_config().as_dict()
    config = PartsConfig.from_dict(data)
    before = config.selector_config_sha
    data["left"]["table"]["base_to_table"][0][3] = 5
    assert config.as_dict()["left"]["table"]["base_to_table"][0][3] == 0
    assert PartsConfig.from_dict(config.as_dict()).selector_config_sha == before


@pytest.mark.parametrize("opening", [0.8, 0.9])
def test_repeated_partial_release_commands_do_not_restart_closure(opening):
    h = Cycle()
    h.enter()
    h.submit(0.1)
    h.ready(z=0.04, position=0.2, effort=0.9)
    assert h.client.arm_snapshots()["left"]["holding_locked"]
    h.submit(opening)
    for _ in range(5):
        snap = h.step(0.08, position=0.2)
        h.submit(opening)
        assert snap["holding_locked"]
        assert "waiting_actual_release" in snap["reason_codes"]
    for _ in range(12):
        snap = h.step(0.08, position=opening)
        h.submit(opening)
    assert not snap["holding_locked"] and snap["actual_open_confirmed"]
    assert h.kinds().count("selector_closure_started") == 1
    assert h.kinds().count("release_confirmed") == 1


def test_new_recording_seeds_holding_history_and_replay_detects_tampering():
    from yam_abc_reproduce.hil.parts.replay import verify_selector

    h = Cycle()
    h.enter()
    h.submit(0.1)
    h.ready(z=0.04, position=0.2, effort=0.9)
    h.client.machine.handback(tick=h.tick, now=h.now)
    h.step(0.08, position=0.2, recording=False)
    rows = []
    for _ in range(5):
        h.step(0.04, position=0.2)
        h.submit(0.1)
        target = h.state.copy()
        target[[6, 13]] = [0.1, 0.1]
        rows.append(
            dict(tick=h.tick, parts=copy.deepcopy(h.client.record()), submitted_action=target)
        )
    assert rows[0]["parts"]["selector_context"]["initial_state"]["lanes"]["left"]["holding"]
    assert verify_selector(rows, h.client.config)["valid"]
    altered = copy.deepcopy(rows)
    altered[2]["parts"]["arms"]["left"]["eligible"] = True
    assert not verify_selector(altered, h.client.config)["valid"]


def test_pause_after_unconfirmed_grasp_recovers_from_new_actual_open_samples():
    h = Cycle()
    h.enter()
    h.submit(0.1)
    h.step(0.04, position=0.1, active=False)
    snap = h.ready(z=0.08, position=0.9)
    assert snap["eligible"] and snap["empty_hand"]
    h.step(0.049)
    assert h.client.machine.active_arm == "left"


def test_xy_translation_is_recorded_but_not_an_eligibility_gate():
    data = mock_config().as_dict()
    data["left"]["table"]["base_to_table"][0][3] = 1.0
    h = Cycle(PartsConfig.from_dict(data))
    h.enter()
    snap = h.client.arm_snapshots()["left"]
    assert snap["position"][0] == 0 and snap["table_position_m"][0] == 1
    assert h.client.machine.active_arm == "left"
    assert "inside_grasp_region" not in snap


def test_repeated_open_samples_after_release_never_confirm_duration():
    h = Cycle()
    h.enter()
    h.submit(0.1)
    h.step(0.04, position=0.1)
    h.submit(0.9)
    stamp = 1000 + h.now + 0.01
    for _ in range(6):
        snap = h.step(0.08, stamp=stamp)
    assert not snap["actual_open_confirmed"]
    assert "feedback_invalid_or_stale" in snap["reason_codes"]
    assert h.client.machine.completed[-1]["result"] == "canceled"


def test_close_begins_above_entry_then_contact_below_entry_exits_to_base():
    h = Cycle()
    h.step(position=0.9)
    h.submit(0.6)
    h.step(position=0.6)
    h.submit(0.3)
    before_entry_close_tick = h.client.selector.lanes["left"]["closure_tick"]
    h.step(0.049, position=0.3)
    assert h.client.machine.active_arm == "left"
    assert not h.client.arm_snapshots()["left"]["actual_open_confirmed"]
    h.submit(0.3)  # No extra closure delta after the crossing.
    for _ in range(6):
        h.step(0.04, position=0.3, effort=-0.8)
        h.submit(0.3)
    assert h.client.machine.phase["left"] == "EXIT_PENDING"
    assert h.client.machine.attempt["closure_tick"] == before_entry_close_tick
    assert h.client.machine.attempt["closure_observed_before_entry"]
    assert h.client.machine.handback(tick=h.tick, now=h.now)
    assert h.client.machine.completed[-1]["result"] == "success"


def test_partial_closed_low_effort_without_any_opening_can_enter():
    h = Cycle()
    h.ready(position=0.4)
    snap = h.step(0.049, position=0.4)
    assert h.client.machine.active_arm == "left"
    assert not snap["actual_open_confirmed"]


def test_holding_survives_epoch_and_low_effort_until_actual_release_confirmation():
    h = Cycle()
    h.enter()
    h.submit(0.1)
    h.ready(z=0.04, position=0.2, effort=0.9)
    h.client.machine.handback(tick=h.tick, now=h.now)
    h.step(0.08, position=0.2, effort=0.1, epoch=2)
    for _ in range(12):
        snap = h.step(0.08, position=0.2, effort=0.1, epoch=2)
    assert snap["holding_locked"] and not snap["eligible"]
    h.step(0.049, position=0.2, effort=0.1, epoch=2)
    assert h.client.machine.attempt is None
    h.submit(0.9)
    for _ in range(12):
        snap = h.step(0.08, position=0.9, effort=0.1, epoch=2)
    assert not snap["holding_locked"] and snap["eligible"]


def test_shadow_missing_table_reports_gaps_collect_rejects_before_control():
    data = mock_config().as_dict()
    data["left"]["table"] = None
    h = Cycle(PartsConfig.from_dict(data))
    h.ready()
    snap = h.step(0.049)
    assert "selector_parameters_unset" in snap["reason_codes"]
    assert h.client.machine.attempt is None
    data["mode"] = "collect"
    with pytest.raises(ValueError, match="table"):
        PartsConfig.from_dict(data).validate_execution()


def test_xy_motion_does_not_change_candidate_or_rewrite_frozen_prefix():
    from yam_abc_reproduce.hil.parts.mock import mock_reply

    data = mock_config("eval").as_dict()
    data["continuity_rad"] = 1.0  # Fixture-only large XY motion, no hardware.
    h = Cycle(PartsConfig.from_dict(data))
    h.enter()
    candidate = mock_reply({"context": {"observation_policy_tick": 0}}, None, h.client.config)
    h.client.replies[(1, 1)] = candidate
    owner = dict(request=dict(epoch=1, request_id=1), model_index=10)
    target, source = h.client.edit(h.tick, h.state, owner, limit_target=lambda x: x)
    h.client.submitted(h.tick, h.now, target, source)
    delta = np.asarray(source["parts"]["physical_residual_rad"])
    timeline = RtcTimeline()
    timeline._committed[h.tick + 5] = (target.copy(), "policy")
    timeline._owners[h.tick + 5] = source
    h.step(0.04, x=0.4)
    carried, source = h.client.edit(h.tick, h.state, owner, limit_target=lambda x: x)
    np.testing.assert_allclose(carried - h.state, delta)
    assert h.client.machine.phase["left"] == "ACTIVE_DESCENT"
    np.testing.assert_array_equal(timeline.select(h.tick + 4, h.state)[0], target)
    assert not h.client.machine.completed
