"""PARTS orchestration at existing policy/RTC/recording boundaries.

All decisions belong to the caller's control owner. Disk work goes through the
bounded journal; this object never polls CAN or sends learner requests.
"""

from __future__ import annotations

import copy
import math
from dataclasses import asdict

import numpy as np

from .config import ARMS, INDICES, SCHEMA
from .height import Heights
from .machine import Attempts
from .protocol import handshake, validate_reply
from .selector import RulesSelector


class PartsClient:
    def __init__(self, config, *, run_id, session_id, emit, kinematics=None):
        config.validate_execution()
        self.config, self.run_id, self.session_id = config, run_id, session_id
        self.emit = emit
        self.heights = Heights(config, kinematics)
        self.machine = Attempts(config, self._emit)
        self.selector = RulesSelector(config, self._selector_event)
        self.requests = {}
        self.replies = {}
        self.declaration = None
        self.capability = "unsupported"
        self.tick, self.now, self.epoch = 0, 0.0, None
        self.last_edit = None
        self._edited = {}
        self._handbacks = set()
        self.error = None
        self._recorded_requests = set()
        self._actor_snapshots = None
        self.preceding_target = None
        self.committed_arms = None
        self.selector_context = None
        self._recording = False

    def cancel_pending(self, reason):
        """Retain unanswered requests, without inventing a reply or feature."""
        for key, item in self.requests.items():
            if key not in self._recorded_requests:
                self._emit(
                    "request",
                    dict(
                        item,
                        error=reason,
                        discarded=True,
                        received_at=None,
                        actions_native=None,
                        parts_reply=None,
                    ),
                )
                self._recorded_requests.add(key)

    def _selector_event(self, kind, value):
        # One event identity/sequence authority for both rules and attempts.
        extra = {k: v for k, v in value.items() if k not in ("kind", "tick", "time")}
        self.machine.event(value["kind"], value["tick"], value["time"], **extra)

    def _emit(self, kind, value):
        value = {"schema": SCHEMA, "run_id": self.run_id, "session_id": self.session_id, **value}
        invalid = []

        def scalar_nulls(item, path=""):
            if isinstance(item, dict):
                return {k: scalar_nulls(v, path + "/" + k) for k, v in item.items()}
            if isinstance(item, (list, tuple)):
                return [scalar_nulls(v, path + "/" + str(i)) for i, v in enumerate(item)]
            if isinstance(item, (float, np.floating)) and not math.isfinite(item):
                invalid.append(path)
                return None
            return item  # Arrays are validated separately before HDF5 storage.

        value = scalar_nulls(value)
        if invalid:
            value["invalid_scalar_fields"] = invalid
        if self.emit(kind, value) is False:
            self.error = "PARTS recording queue overflow/write failure"

    def observe(self, *, tick, now, epoch, state, ages, feedback, policy_active, recording=True):
        self.tick, self.now = tick, now
        self.machine.event_refs = []
        if self.epoch is not None and epoch != self.epoch:
            self.cancel_pending("epoch_changed")
            self.replies.clear()
            self._edited.clear()
            self._handbacks.clear()
        self.epoch = epoch
        height = self.heights.sample(state, sampled_at=now, feedback_age_s=ages, now=now)
        force = {arm: feedback[i] for i, arm in enumerate(ARMS)}
        committed = [] if self.committed_arms is None else sorted(self.committed_arms())
        self.selector_context = dict(
            epoch=epoch,
            control_time=now,
            policy_active=policy_active,
            phases=dict(self.machine.phase),
            active_arm=self.machine.active_arm,
            committed_arms=committed,
            consumed_arm=None,
            initial_state=self.selector.checkpoint() if recording and not self._recording else None,
        )
        self._recording = recording
        eligibility = self.selector.observe(
            tick=tick,
            now=now,
            epoch=epoch,
            heights=height,
            feedback=force,
            policy_active=policy_active,
            phases=self.machine.phase,
            active_arm=self.machine.active_arm,
            committed_arms=committed,
        )
        self.machine.eligibility = eligibility
        preceding_attempt = self.machine.attempt
        self.machine.observe(
            tick=tick,
            now=now,
            epoch=epoch,
            heights=height,
            feedback=force,
            policy_active=policy_active,
            preserve_events=True,
        )
        if self.machine.attempt is not None and self.machine.attempt is not preceding_attempt:
            self.selector.consume(self.machine.active_arm, tick, now)
            self.selector_context["consumed_arm"] = self.machine.active_arm
        arm = self.machine.active_arm
        if (
            arm is not None
            and self.machine.phase[arm] == "ACTIVE_DESCENT"
            and eligibility[arm]["holding_locked"]
        ):
            # Closing may start before crossing 50 mm. The selector tracks the
            # real command history; do not require another close command inside
            # the attempt to recognize its subsequent confirmed contact.
            self.machine.attempt.update(
                closure_tick=eligibility[arm]["closure_tick"],
                closure_time=eligibility[arm]["closure_time"],
                closure_observed_before_entry=(
                    eligibility[arm]["closure_tick"] < self.machine.attempt["entry_tick"]
                ),
                reward_proposal_tick=tick,
            )
            self.machine.exit_pending("success", "force_confirmed", tick, now)

    def build_request(
        self, token, *, observation_tick, commitment=None, scheduler=None, observation=None
    ):
        context = dict(
            run_id=self.run_id,
            session_id=self.session_id,
            epoch=token.epoch,
            request_id=token.request_id,
            observation_id=token.observation_id,
            observation_policy_tick=observation_tick,
        )
        arms = self.arm_snapshots()
        payload = dict(
            protocol="yam-parts-v1",
            contract_sha=self.config.contract_sha,
            mode=self.config.mode,
            context=context,
            active_arm=self.machine.active_arm,
            arms=arms,
            scheduler=scheduler or {},
        )
        item = dict(
            context=context,
            parts_request=payload,
            sent_at=self.now,
            observation_state=None if observation is None else observation.get("observation.state"),
            observation_tick=observation_tick,
            committed_prefix=None if commitment is None else commitment.actions.copy(),
            committed_sources=(scheduler or {}).get("committed_sources", []),
            video_refs=None,
            video_reference_reason="resolved_from_recorded_observation_id_on_finalize",
        )
        self.requests[(token.epoch, token.request_id)] = copy.deepcopy(item)
        while len(self.requests) > 64:
            oldest = next(iter(self.requests))
            if oldest not in self._recorded_requests:
                self._emit(
                    "request",
                    dict(
                        self.requests[oldest],
                        error="request_history_evicted",
                        discarded=True,
                        received_at=None,
                    ),
                )
            self.requests.pop(oldest)
            self._recorded_requests.discard(oldest)
        return payload

    def accept_reply(self, reply, *, metadata, discarded=False):
        key = (reply.token.epoch, reply.token.request_id)
        if key in self._recorded_requests:
            self._emit(
                "event",
                dict(
                    kind="late_or_duplicate_reply",
                    tick=self.tick,
                    time=self.now,
                    context=asdict(reply.token),
                    event_seq=0,
                ),
            )
            return
        item = copy.deepcopy(self.requests.get(key))
        if item is None:
            self._emit(
                "event",
                dict(
                    kind="reply_without_request",
                    tick=self.tick,
                    time=self.now,
                    context=asdict(reply.token),
                    event_seq=0,
                ),
            )
            return
        item.update(
            actions_native=reply.actions,
            parts_reply=reply.parts,
            received_at=self.now,
            error=reply.error,
            discarded=discarded,
            capability="advertised"
            if isinstance(metadata, dict) and metadata.get("parts")
            else "unsupported",
        )
        try:
            self.declaration = handshake(metadata, self.config)
            self.capability = "unsupported" if self.declaration is None else "supported"
            if not discarded and reply.error is None:
                candidate = validate_reply(
                    reply.parts,
                    item["parts_request"],
                    self.declaration,
                    self.config,
                    delay_steps=reply.token.rtc_delay_steps or 0,
                )
                snapshots = {arm: candidate["candidates"][arm]["actor_snapshot_id"] for arm in ARMS}
                if self._actor_snapshots is not None and snapshots != self._actor_snapshots:
                    raise ValueError("PARTS actor snapshot changed within run")
                self._actor_snapshots = snapshots
                # A token's fixed action timeline is never shifted to accommodate lateness.
                takeover = item["observation_tick"] + (reply.token.rtc_delay_steps or 0)
                if reply.token.rtc_delay_steps and self.tick > takeover:
                    raise ValueError("PARTS late reply")
                self.replies[key] = candidate
                while len(self.replies) > 16:
                    self.replies.pop(next(iter(self.replies)))
        except (ValueError, TypeError, KeyError) as exc:
            item["parts_error"] = str(exc)
            if self.config.mode in ("collect", "eval") and not discarded:
                self.machine.cancel("invalid_parts_reply", self.tick, self.now)
                self._emit("request", item)
                self._recorded_requests.add(key)
                raise ValueError(str(exc)) from exc
        self._emit("request", item)
        self._recorded_requests.add(key)

    def arm_snapshots(self):
        result = {}
        for arm in ARMS:
            active = self.machine.active_arm == arm
            force_valid = self.machine.fresh_force(arm, self.now)
            confirmation = (
                0
                if not active
                or self.machine.confirm_started is None
                or self.machine.confirm_last is None
                else max(0, self.machine.confirm_last - self.machine.confirm_started)
            )
            result[arm] = dict(
                self.machine.heights.get(arm, {}),
                attempt_id=None
                if self.machine.active_arm != arm
                else self.machine.attempt["attempt_id"],
                phase=self.machine.phase[arm],
                **self.machine.eligibility[arm],
                entry_armed=self.machine.entry_armed[arm],
                force_feedback=self.machine.feedback.get(arm),
                elapsed_s=max(0, self.now - self.machine.attempt["entry_time"]) if active else 0,
                confirmation_s=confirmation,
                force_valid=bool(force_valid),
                effort_nm=self.machine.feedback[arm]["effort_nm"] if force_valid else None,
            )
        return result

    def _previous_final_target(self, tick):
        """Future prefix checks compare adjacent final targets, not live pose."""
        if self.preceding_target is not None:
            target = self.preceding_target(tick - 1)
            if target is not None:
                return np.asarray(target)
        for key, (target, _) in reversed(self._edited.items()):
            if key[0] == self.epoch and key[1] == tick - 1:
                return target
        previous = self.machine.previous_command
        return None if previous is None else np.asarray(previous["target"])

    def edit(self, tick, base, owner, *, limit_target):
        """Edit only a not-yet-committed target; RtcTimeline bypasses this for prefixes."""
        base = np.asarray(base, dtype=float).copy()
        source = copy.deepcopy(owner or {})
        key = (
            self.epoch,
            tick,
            (source.get("request") or {}).get("request_id"),
            None if self.machine.attempt is None else self.machine.attempt["attempt_id"],
        )
        if key in self._edited:
            target, record = self._edited[key]
            source["parts"] = copy.deepcopy(record)
            return target.copy(), source
        target = base.copy()
        record = dict(
            schema=SCHEMA,
            run_id=self.run_id,
            mode=self.config.mode,
            contract_sha=self.config.contract_sha,
            active_arm=self.machine.active_arm,
            attempt_id=None if self.machine.attempt is None else self.machine.attempt["attempt_id"],
            phase=None
            if self.machine.active_arm is None
            else self.machine.phase[self.machine.active_arm],
            selection={k: v for k, v in source.items() if k != "parts"},
            residual_applied=False,
            physical_residual_rad=[0.0] * 14,
            constraints=[],
            base_target=base.tolist(),
            candidate_ref=None,
        )
        arm = self.machine.active_arm
        token = source.get("request") or {}
        candidate = self.replies.get((token.get("epoch"), token.get("request_id")))
        returning = (
            arm is not None
            and candidate is not None
            and self.machine.phase[arm] == "EXIT_PENDING"
            and candidate["context"]["observation_policy_tick"]
            >= self.machine.attempt["exit_requested_tick"]
        )
        if returning:
            # Shadow also evaluates the handback boundary, without editing base.
            previous = self._previous_final_target(tick)
            gap = (
                0
                if previous is None
                else np.max(abs(base[list(INDICES[arm])] - previous[list(INDICES[arm])]))
            )
            if self.config.mode in ("collect", "eval") and gap > self.config.continuity_rad:
                raise ValueError("PARTS handback discontinuity")
            self._handbacks.add((tick, self.machine.attempt["attempt_id"]))
        if arm is not None and self.config.mode in ("collect", "eval"):
            index = source.get("model_index")
            if candidate is None or index is None:
                record["constraints"].append("candidate_missing")
            else:
                value = candidate["candidates"][arm]
                if returning:
                    record["constraints"].append("handback_to_new_base")
                elif value["editable_mask"][index]:
                    target[list(INDICES[arm])] += value["B_rad"] * value["u"][index]
                    record["candidate_ref"] = dict(
                        request_epoch=token.get("epoch"),
                        request_id=token.get("request_id"),
                        model_index=index,
                        arm=arm,
                        actor_snapshot_id=value["actor_snapshot_id"],
                        behavior_snapshot_id=candidate["behavior_snapshot_id"],
                    )
                    bounded = np.asarray(limit_target(target), dtype=float)
                    if bounded.shape != (14,) or not np.isfinite(bounded).all():
                        raise ValueError("PARTS combined target invalid")
                    if not np.array_equal(target, bounded):
                        record["constraints"].append("hard_limits")
                    target = bounded
                    other = [i for i in range(14) if i not in INDICES[arm]]
                    if not np.array_equal(target[other], base[other]):
                        raise ValueError("PARTS edited inactive arm or gripper")
                    previous_target = self._previous_final_target(tick)
                    if (
                        previous_target is not None
                        and np.max(
                            abs(target[list(INDICES[arm])] - previous_target[list(INDICES[arm])])
                        )
                        > self.config.continuity_rad
                    ):
                        raise ValueError("PARTS joint continuity boundary")
                    pose = self.heights.sample(
                        target, sampled_at=self.now, feedback_age_s=[0, 0], now=self.now
                    )[arm]
                    if (
                        not pose["height_valid"]
                        or pose["height_m"] < getattr(self.config, arm).minimum_height_m
                    ):
                        raise ValueError("PARTS combined height boundary")
                    record["physical_residual_rad"] = (target - base).tolist()
                    record["residual_applied"] = bool(np.any(target != base))
                else:
                    record["constraints"].append("not_editable")
        # Shadow follows the existing path exactly, including gripper transforms.
        if self.config.mode == "shadow" and not np.array_equal(target, base):
            raise AssertionError("PARTS shadow changed action")
        self._edited[key] = (target.copy(), copy.deepcopy(record))
        while len(self._edited) > 128:
            self._edited.pop(next(iter(self._edited)))
        source["parts"] = record
        return target, source

    def submitted(self, tick, now, target, selection, *, error=None):
        source = (selection or {}).get("parts")
        residual = np.zeros(14) if source is None else np.asarray(target) - source["base_target"]
        if source is not None:
            source = copy.deepcopy(source)
            source["physical_residual_rad"] = residual.tolist()
            source["residual_applied"] = bool(np.any(residual))
        self.last_edit = source
        if error:
            self.machine.cancel(error, tick, now)
        elif self.machine.attempt and (tick, self.machine.attempt["attempt_id"]) in self._handbacks:
            self.machine.handback(tick=tick, now=now)
        self._handbacks = {value for value in self._handbacks if value[0] > tick}
        self.machine.submitted(
            tick=tick,
            now=now,
            target=target,
            selection={k: v for k, v in (selection or {}).items() if k != "parts"},
            residual=residual,
        )
        self.selector.submitted(tick=tick, now=now, target=target)

    def record(self):
        arm = self.machine.active_arm
        result = dict(
            schema=SCHEMA,
            run_id=self.run_id,
            mode=self.config.mode,
            contract_sha=self.config.contract_sha,
            capability=self.capability,
            active_arm=arm,
            attempt_id=None if self.machine.attempt is None else self.machine.attempt["attempt_id"],
            phase=None if arm is None else self.machine.phase[arm],
            arms=self.arm_snapshots(),
            eligible=None if arm is None else self.machine.eligibility[arm],
            selection=None,
            physical_residual_rad=[0.0] * 14,
            residual_applied=False,
            constraints=[],
            reward=copy.deepcopy(self.machine.reward),
            event_refs=list(self.machine.event_refs),
            recording_error=self.error,
            parameter_gaps=self.config.gaps(),
        )
        result["config"] = self.config.as_dict()
        result["selector_schema"] = self.config.selector["schema"]
        result["selector_config_sha"] = self.config.selector_config_sha
        result["selector_context"] = copy.deepcopy(self.selector_context)
        if self.last_edit:
            for key in (
                "selection",
                "physical_residual_rad",
                "residual_applied",
                "constraints",
                "candidate_ref",
            ):
                result[key] = self.last_edit.get(key)
        return result
