"""Deterministic grasp eligibility; feedback and submitted commands, never IO.

Holding is a historical latch, not the inverse of the instantaneous torque
test. Only a new release command followed by advancing open-position samples
can clear it. Attempt ownership and RTC commitment remain separate inputs.
"""

from __future__ import annotations

import copy
import math
from numbers import Real

from .config import ARMS


def fresh_feedback(value, now, max_age, *, position=False):
    if not value or not value.get("valid") or max_age is None:
        return False
    keys = ["effort_nm", "sdk_updated_at", "sampled_at", "feedback_age_s"]
    if position:
        keys.append("position")
    if any(
        not isinstance(value.get(k), Real)
        or isinstance(value[k], bool)
        or not math.isfinite(value[k])
        for k in keys
    ):
        return False
    elapsed = now - value["sampled_at"]
    return 0 <= elapsed <= max_age and 0 <= value["feedback_age_s"] + elapsed <= max_age


class Confirmation:
    """Repeated SDK snapshots never contribute duration; gaps break a window."""

    def __init__(self):
        self.reset()

    def reset(self, stamp=None):
        self.stamp, self.started, self.last = stamp, None, None
        self.count = 0

    def update(self, feedback, now, good, duration, max_gap, *, min_samples=1):
        stamp = feedback["sdk_updated_at"]
        if self.stamp is not None and stamp <= self.stamp:
            return False
        self.stamp = stamp
        if not good or duration is None or max_gap is None:
            self.started = self.last = None
            self.count = 0
            return False
        if self.last is None or now - self.last > max_gap:
            self.started = now
            self.count = 0
        self.last = now
        self.count += 1
        # 10 samples at 30 Hz span 0.3 s inclusive. Tolerate only floating-point
        # clock subtraction error, not a shorter confirmation window.
        return self.count >= min_samples and now - self.started + 1e-9 >= duration


class RulesSelector:
    def __init__(self, config, emit):
        self.config, self.emit = config, emit
        self.epoch, self.active_control = None, False
        self.lanes = {
            arm: dict(
                state="UNKNOWN",
                holding=False,
                requires_release=False,
                closing=False,
                closure_tick=None,
                closure_time=None,
                release_tick=None,
                release_time=None,
                anchor=None,
                reopen_anchor=None,
                preceding_command=None,
                opened=False,
                loss_reported=False,
                open_window=Confirmation(),
                force_window=Confirmation(),
                feedback=None,
                pose=None,
            )
            for arm in ARMS
        }
        self.output = {}

    def checkpoint(self):
        """Once per recording start, so replay need not invent pre-episode history."""
        lanes = {}
        for arm, lane in self.lanes.items():
            lanes[arm] = {
                k: copy.deepcopy(v)
                for k, v in lane.items()
                if k not in ("feedback", "pose", "open_window", "force_window")
            }
            for name in ("open_window", "force_window"):
                lanes[arm][name] = {
                    k: getattr(lane[name], k) for k in ("stamp", "started", "last", "count")
                }
        return dict(
            schema=self.config.selector["schema"],
            config_sha=self.config.selector_config_sha,
            epoch=self.epoch,
            active_control=self.active_control,
            lanes=lanes,
        )

    def restore_for_replay(self, checkpoint):
        """Offline-only seeding; never called by a device/control operation."""
        if (
            checkpoint["schema"] != self.config.selector["schema"]
            or checkpoint["config_sha"] != self.config.selector_config_sha
        ):
            raise ValueError("selector_checkpoint_contract_mismatch")
        self.epoch, self.active_control = checkpoint["epoch"], checkpoint["active_control"]
        for arm in ARMS:
            saved, lane = checkpoint["lanes"][arm], self.lanes[arm]
            for key in tuple(lane):
                if key in ("feedback", "pose"):
                    continue
                if key in ("open_window", "force_window"):
                    for field in ("stamp", "started", "last", "count"):
                        setattr(lane[key], field, saved[key][field])
                else:
                    lane[key] = copy.deepcopy(saved[key])
        self.output = {}

    def _event(self, arm, kind, tick, now, **extra):
        lane = self.lanes[arm]
        self.emit(
            "event",
            dict(
                kind=kind,
                arm=arm,
                tick=tick,
                time=now,
                selector_schema=self.config.selector["schema"],
                selector_config_sha=self.config.selector_config_sha,
                feedback=lane["feedback"],
                pose=lane["pose"],
                preceding_gripper_command=lane["preceding_command"],
                **extra,
            ),
        )

    def _state(self, arm, value, tick, now):
        lane = self.lanes[arm]
        if lane["state"] != value:
            before, lane["state"] = lane["state"], value
            self._event(
                arm, "selector_state_changed", tick, now, previous_state=before, state=value
            )

    def invalidate(self, arm, tick, now, reason):
        lane = self.lanes[arm]
        lane["open_window"].reset()
        lane["force_window"].reset()
        lane.update(
            opened=False,
            closing=False,
            closure_tick=None,
            closure_time=None,
            release_tick=None,
            release_time=None,
            anchor=None,
            reopen_anchor=None,
            preceding_command=None,
            requires_release=lane["holding"],
        )
        # Holding survives a gap. Empty readiness instead resynchronizes from
        # fresh height/effort; old samples/commands are never reused.
        self._state(arm, "UNKNOWN", tick, now)
        if self.output.get(arm, {}).get("reason_codes") != [reason]:
            self._event(arm, "selector_unsynchronized", tick, now, reason=reason)

    def consume(self, arm, tick, now):
        """New release proof must follow this attempt, never a pre-grasp opening."""
        lane = self.lanes[arm]
        lane.update(requires_release=True, opened=False, release_tick=None, release_time=None)
        lane["open_window"].reset((lane["feedback"] or {}).get("sdk_updated_at"))
        self.output[arm].update(
            eligible=False,
            empty_hand=False,
            rearm_ready=False,
            actual_open_confirmed=False,
            reason_codes=["attempt_active"],
        )
        self._event(arm, "selector_attempt_consumed", tick, now)

    def observe(
        self,
        *,
        tick,
        now,
        epoch,
        heights,
        feedback,
        policy_active,
        phases,
        active_arm,
        committed_arms=(),
    ):
        changed = self.epoch is not None and epoch != self.epoch
        resumed = policy_active and not self.active_control
        self.epoch, self.active_control = epoch, policy_active
        for arm in ARMS:
            lane, options = self.lanes[arm], getattr(self.config, arm)
            lane["feedback"], lane["pose"] = feedback.get(arm), heights[arm]
            f, h = lane["feedback"], lane["pose"]
            gaps = self.config.selector_gaps(arm)
            valid = fresh_feedback(f, now, self.config.max_feedback_age_s, position=True)
            reason = (
                "control_not_policy"
                if not policy_active
                else "epoch_changed"
                if changed
                else "selector_parameters_unset"
                if gaps
                else "feedback_invalid_or_stale"
                if not valid or not h.get("height_valid")
                else None
            )
            if reason or resumed:
                self.invalidate(arm, tick, now, reason or "control_resumed")
            if reason:
                self.output[arm] = self._snapshot(arm, False, False, [reason], gaps=gaps)
                continue
            # Actual position is preserved even when outside nominal [0,1].
            open_now = f["position"] >= options.open_position_min
            window = lane["open_window"]
            release_evidence = (
                lane["release_tick"] is not None and f["sampled_at"] >= lane["release_time"]
            )
            can_open = not lane["requires_release"] and not lane["holding"] or release_evidence
            if window.update(
                f,
                now,
                open_now and can_open,
                options.open_confirm_s,
                self.config.max_confirmation_gap_s,
                min_samples=options.open_confirm_samples,
            ):
                lane["opened"] = True
                if release_evidence:
                    lane.update(
                        holding=False,
                        requires_release=False,
                        closing=False,
                        closure_tick=None,
                        closure_time=None,
                        release_tick=None,
                        release_time=None,
                        loss_reported=False,
                        anchor=None,
                        reopen_anchor=None,
                    )
                    lane["force_window"].reset(f["sdk_updated_at"])
                    self._event(arm, "release_confirmed", tick, now)
                    self._state(arm, "REARM_WAIT", tick, now)
            elif not open_now:
                lane["opened"] = False
            if lane["closing"] and lane["release_tick"] is None:
                if lane["force_window"].update(
                    f,
                    now,
                    abs(f["effort_nm"]) > 0.65,
                    self.config.confirm_s,
                    self.config.max_confirmation_gap_s,
                ):
                    if not lane["holding"]:
                        lane["holding"] = True
                        self._event(arm, "holding_locked", tick, now)
                    self._state(arm, "HOLDING", tick, now)
            if lane["holding"] and abs(f["effort_nm"]) <= 0.65:
                if not lane["loss_reported"]:
                    lane["loss_reported"] = True
                    self._event(arm, "post_grasp_loss", tick, now)
            elif abs(f["effort_nm"]) > 0.65:
                lane["loss_reported"] = False
            above = h["height_m"] > options.h_entry_m + options.entry_hysteresis_m
            active = active_arm == arm
            low_effort = abs(f["effort_nm"]) <= 0.65
            # Opening is release proof for a held object, NOT a prerequisite
            # for approaching a new grasp: the policy may already be closing.
            rearm = not lane["holding"] and low_effort
            blocked = arm in committed_arms
            if lane["holding"]:
                self._state(arm, "RELEASE_WAIT" if release_evidence else "HOLDING", tick, now)
            elif rearm and above and not active and not blocked:
                self._state(arm, "EMPTY_READY", tick, now)
            elif not active and lane["state"] != "EMPTY_READY":
                self._state(arm, "REARM_WAIT", tick, now)
            reasons = []
            if lane["holding"]:
                reasons.append("holding_object")
            elif not low_effort:
                reasons.append("gripper_effort_high")
            if lane["holding"] and release_evidence:
                reasons.append("waiting_actual_release")
            if lane["holding"] and not lane["opened"]:
                reasons.append("waiting_actual_open")
            if lane["holding"] and not release_evidence:
                reasons.append("waiting_new_release_command")
            if not above and lane["state"] != "EMPTY_READY":
                reasons.append("waiting_retract")
            if active:
                reasons.append("attempt_active")
            if blocked:
                reasons.append("old_residual_committed")
            eligible = (
                lane["state"] == "EMPTY_READY"
                and not active
                and not blocked
                and low_effort
                and phases[arm] in ("READY", "WAIT_REARM")
            )
            previous = self.output.get(arm, {}).get("eligible")
            self.output[arm] = self._snapshot(
                arm,
                eligible,
                rearm,
                reasons,
                rearm_ready=bool(rearm and above and not active and not blocked),
            )
            if previous != eligible:
                self._event(
                    arm,
                    "eligibility_changed",
                    tick,
                    now,
                    eligible=bool(eligible),
                    reason_codes=reasons,
                )
        return self.output

    def _snapshot(self, arm, eligible, empty, reasons, **extra):
        lane = self.lanes[arm]
        result = dict(
            eligible=bool(eligible),
            empty_hand=bool(empty),
            source=self.config.selector["schema"],
            selector_state=lane["state"],
            selector_schema=self.config.selector["schema"],
            selector_config_sha=self.config.selector_config_sha,
            reason_codes=reasons,
            holding_locked=lane["holding"],
            actual_open_confirmed=lane["opened"],
            closing_detected=lane["closing"],
            closure_tick=lane["closure_tick"],
            closure_time=lane["closure_time"],
            rearm_ready=False,
        )
        result.update(extra)
        return result

    def submitted(self, *, tick, now, target):
        for arm, index in (("left", 6), ("right", 13)):
            lane = self.lanes[arm]
            if not self.active_control or not fresh_feedback(
                lane["feedback"], now, self.config.max_feedback_age_s, position=True
            ):
                continue
            if self.config.close_delta is None or self.config.release_position is None:
                continue
            position = float(target[index])
            if lane["anchor"] is None:
                lane["anchor"] = lane["feedback"]["position"]
            lane["anchor"] = max(lane["anchor"], position)
            if (not lane["closing"] or lane["release_tick"] is not None) and lane[
                "anchor"
            ] - position >= self.config.close_delta:
                lane.update(
                    closing=True,
                    closure_tick=tick,
                    closure_time=now,
                    requires_release=True,
                    opened=False,
                    release_tick=None,
                    release_time=None,
                    reopen_anchor=position,
                )
                lane["open_window"].reset(lane["feedback"]["sdk_updated_at"])
                lane["force_window"].reset(lane["feedback"]["sdk_updated_at"])
                self._event(arm, "selector_closure_started", tick, now)
            lane["reopen_anchor"] = min(
                position,
                lane["feedback"]["position"]
                if lane["reopen_anchor"] is None
                else lane["reopen_anchor"],
            )
            if (
                (lane["requires_release"] or lane["holding"])
                and lane["release_tick"] is None
                and position >= self.config.release_position
                and (
                    position - lane["reopen_anchor"] >= self.config.close_delta
                    or lane["preceding_command"] is None
                )
            ):
                # A partially-open release target may be below the previous
                # fully-open anchor. Start a new command epoch here, otherwise
                # resubmitting that same release looks like another closure.
                lane.update(release_tick=tick, release_time=now, opened=False, anchor=position)
                lane["open_window"].reset(lane["feedback"]["sdk_updated_at"])
                self._state(arm, "RELEASE_WAIT", tick, now)
                self._event(arm, "release_requested", tick, now)
            lane["preceding_command"] = dict(tick=tick, time=now, position=position)
