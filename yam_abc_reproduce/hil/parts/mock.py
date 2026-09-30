"""Deterministic no-SDK PARTS/RTC sample producer for protocol acceptance."""

from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from ..core import Request
from ..policy import Reply
from ..rtc_timeline import RtcTimeline
from ..storage import SegmentWriter
from .config import ARMS, INDICES, PartsConfig
from .controller import PartsClient
from .recording import PartsJournal, finalize


class MockKinematics:
    """Fixture geometry only: joint 1 encodes metres; never a YAM mapping."""

    def forward(self, q):
        poses = {}
        for arm, start in (("left", 0), ("right", 7)):
            pose = np.eye(4)
            pose[2, 3] = q[start]
            poses[arm] = pose
        return SimpleNamespace(**poses)


def mock_config(mode="shadow"):
    table = dict(
        base_to_table=np.eye(4).tolist(),
        normal=[0, 0, 1],
        point=[0, 0, 0],
        frame="mock_table",
        calibration_id="fixture_only",
    )
    side = dict(
        h_entry_m=0.05,
        h_goal_m=0.02,
        minimum_height_m=-0.05,
        budget_s=4,
        table=table,
        B_rad=[0.01] * 6,
    )
    return PartsConfig.from_dict(
        dict(
            mode=mode,
            contract_sha="mock-contract-v1",
            behavior_snapshot_id="mock-behavior-v1",
            behavior_manifest_ref="mock://behavior-v1",
            feature_schema_id="mock-feature-v1",
            confirm_s=0.1,
            max_feedback_age_s=0.1,
            max_pose_age_s=0.1,
            max_confirmation_gap_s=0.07,
            continuity_rad=0.1,
            close_delta=0.02,
            release_position=0.7,
            left=side,
            right=side,
            reward=dict(
                schema="mock-height-grasp-v1",
                formula="negative_absolute_height_error_v1",
                height_weight=1.0,
                grasp_weight=1.0,
            ),
        )
    )


def mock_metadata(config):
    return dict(
        parts=dict(
            protocol="yam-parts-v1",
            contract_sha=config.contract_sha,
            supported_modes=["shadow", "collect", "eval"],
            residual_space="joint_delta_rad",
            horizon=50,
            state_dim=14,
            action_dt=1 / 30,
            feature_schema_id=config.feature_schema_id,
            behavior_manifest_ref=config.behavior_manifest_ref,
            per_arm={
                a: dict(indices=list(INDICES[a]), B_rad=list(getattr(config, a).B_rad))
                for a in ARMS
            },
        )
    )


def mock_reply(request, actions, config, delay_steps=9):
    mask = np.arange(50) >= delay_steps
    return dict(
        protocol="yam-parts-v1",
        contract_sha=config.contract_sha,
        mode=config.mode,
        context=request["context"],
        behavior_snapshot_id=config.behavior_snapshot_id,
        candidates={
            a: dict(
                u=np.full((50, 6), -0.05),
                B_rad=np.asarray(getattr(config, a).B_rad),
                editable_mask=mask.copy(),
                actor_snapshot_id="mock-" + a,
                exploration_applied=config.mode == "collect",
            )
            for a in ARMS
        },
        features=dict(
            feature_schema_id=config.feature_schema_id,
            z=np.arange(8, dtype=np.float32),
            shape=[8],
            dtype="float32",
            feature_ref=None,
        ),
    )


def produce(path, *, producer_sha, mode="shadow"):
    """Full 3-camera/HDF5 fixture with success, failure and cancellation retained."""
    path = Path(path)
    config = mock_config(mode)
    manifest = dict(
        run_id=path.name,
        session_id="mock_session",
        task="mock descent grasp",
        mode=mode,
        mock=True,
        control_hz=30,
        action_dt=1 / 30,
        layout_group_id="mock_layout",
        split_role="mock",
        contract_sha=config.contract_sha,
        config=config.as_dict(),
        behavior_manifest_ref=config.behavior_manifest_ref,
        feature_schema_id=config.feature_schema_id,
        reward_fixed=True,
        fk_model="fixture_only_not_YAM",
        pose_ref="mock_grasp_site",
    )
    journal = PartsJournal(path, manifest)
    client = PartsClient(
        config,
        run_id=path.name,
        session_id="mock_session",
        emit=journal.submit,
        kinematics=MockKinematics(),
    )
    episode = path / "episodes" / "episode_000001"
    episode.mkdir(parents=True)
    writer = SegmentWriter(
        episode,
        30,
        {"mock": True, "rtc": True, "policy_fusion": "rtc"},
        min_free_bytes=0,
        video_backend="libx264",
    )
    metadata = mock_metadata(config)
    timeline = RtcTimeline(delay_steps=9)
    client.preceding_target = timeline.final_target_at
    timeline.edit_target = lambda tick, base, owner: client.edit(
        tick, base, owner, limit_target=lambda x: x
    )
    target = np.zeros(14)
    target[[0, 7]] = 0.09
    target[[6, 13]] = 0.9
    pending = None
    request_id = 0
    client.machine.mark("left", eligible=True, empty_hand=True)
    client.machine.mark("right", eligible=True, empty_hand=True)
    for tick in range(210):
        now = tick / 30
        state = target.copy()
        feedback = [
            dict(
                position=state[j],
                velocity=0,
                effort_nm=(0.8 if tick >= 35 else 0.1) if arm == "left" else 0.2,
                sdk_updated_at=1000 + now,
                sampled_at=now,
                feedback_age_s=0.005,
                valid=True,
            )
            for arm, j in (("left", 6), ("right", 13))
        ]
        client.observe(
            tick=tick,
            now=now,
            epoch=1,
            state=state,
            ages=[0, 0],
            feedback=feedback,
            policy_active=True,
        )
        if tick == 165:
            client.machine.cancel("mock_operator_takeover", tick, now)
            timeline.clear()  # Real takeover clears commitments through Arbiter.
        if pending and tick == pending[0]:
            _, token, commitment, actions, payload = pending
            response = Reply(token, actions, parts=mock_reply(payload, actions, config))
            client.accept_reply(response, metadata=metadata)
            timeline.install(
                commitment,
                actions,
                current_tick=tick,
                limit_target=lambda x: x,
                request=asdict(token),
            )
            pending = None
        target, source = timeline.select(tick, state)
        selection = timeline.last_selection
        client.submitted(tick, now, target, selection)
        timeline.record_submitted(tick, target)
        if tick % 10 == 0:
            commitment = timeline.prepare(
                observation_tick=tick, current_tick=tick, limit_target=lambda x: x
            )
            request_id += 1
            token = Request(
                epoch=1,
                request_id=request_id,
                observation_id=tick,
                created_at=now,
                observed_at=now,
                observation_policy_tick=tick,
                rtc_delay_steps=9,
            )
            payload = client.build_request(
                token,
                observation_tick=tick,
                commitment=commitment,
                scheduler=dict(
                    committed_sources=timeline.prefix_sources(tick, 9),
                    **timeline.scheduler_snapshot(tick),
                ),
                observation={"observation.state": state},
            )
            actions = np.tile(target, (50, 1))
            for i in range(9, 50):
                absolute_tick = tick + i
                # Repeat approach/open cycles. No robot hardware is constructed.
                phase = absolute_tick % 70
                actions[i, [0, 7]] = (
                    max(0.02, 0.09 - phase * 0.003) if phase < 32 else 0.02 if phase < 52 else 0.09
                )
                actions[i, [6, 13]] = 0.1 if 28 <= phase < 52 else 0.9
            actions[:9] = commitment.actions
            pending = (tick + 2, token, commitment, actions, payload)
        images = {
            a: np.full((64, 96, 3), 40 + (tick % 100), dtype=np.uint8)
            for a in ("top", "left", "right")
        }
        writer.append(
            dict(
                tick=tick,
                time=now,
                epoch=1,
                source=source,
                obs_id=tick,
                observation_valid=True,
                measured_state=state,
                observation_state=state,
                submitted_action=target,
                selected_action=target,
                policy_action=target,
                policy_selection=selection,
                parts=client.record(),
                grasp_diagnostics=dict(sample_phase="before_command", followers=feedback),
            ),
            images,
        )
    client.machine.cancel("mock_run_closed", 210, 7)
    client.cancel_pending("mock_run_closed")
    writer.close("unknown")
    journal.close()
    publication = finalize(path, episodes=[episode], producer_sha=producer_sha, gaps=journal.gaps)
    if not publication["client_complete"]:
        raise ValueError(publication["gaps"])
    return publication
