"""Local single-active-arm grasp attempts, using advancing SDK feedback only."""

from __future__ import annotations

import math
import uuid
from collections import deque

from .config import ARMS
from .reward import components


class Attempts:
    def __init__(self, config, emit):
        self.config, self.emit = config, emit
        self.phase = dict.fromkeys(ARMS, "READY")
        self.eligibility = {
            a: dict(eligible=False, empty_hand=False, source="explicit_marker") for a in ARMS
        }
        self.previous = {}
        self.attempt = None
        self.completed = deque(maxlen=64)  # Complete history belongs to the journal.
        self.priority = "left"
        self.sequence = 0
        self.last_feedback = {}
        self.confirm_started = None
        self.confirm_last = None
        self.previous_command = None
        self.heights = {}
        self.feedback = {}
        self.epoch = None
        self.event_refs = []
        self.reward = components(config, error_m=None)
        self._reward_tick = None
        self.closure_anchor = self.reopen_anchor = None

    def mark(self, arm, *, eligible, empty_hand):
        if arm not in ARMS:
            raise ValueError("PARTS arm must be left/right")
        self.eligibility[arm] = dict(
            eligible=bool(eligible), empty_hand=bool(empty_hand), source="explicit_marker"
        )

    def event(self, kind, tick, now, **extra):
        self.sequence += 1
        value = dict(
            event_id=uuid.uuid4().hex,
            event_seq=self.sequence,
            kind=kind,
            tick=tick,
            time=now,
            arm=self.active_arm,
            attempt_id=None if self.attempt is None else self.attempt["attempt_id"],
            preceding_command=self.previous_command,
            **extra,
        )
        self.emit("event", value)
        self.event_refs.append(value["event_id"])
        return value["event_id"]

    @property
    def active_arm(self):
        return None if self.attempt is None else self.attempt["arm"]

    def fresh_force(self, arm, now):
        f = self.feedback.get(arm)
        if not f or not f.get("valid") or self.config.max_feedback_age_s is None:
            return False
        numbers = [
            f.get(k) for k in ("effort_nm", "sdk_updated_at", "sampled_at", "feedback_age_s")
        ]
        if any(v is None or not math.isfinite(v) for v in numbers):
            return False
        return (
            0 <= now - f["sampled_at"] <= self.config.max_feedback_age_s
            and 0 <= f["feedback_age_s"] + now - f["sampled_at"] <= self.config.max_feedback_age_s
        )

    def _terminate(self, result, reason, tick, now, *, handback_tick=None):
        if self.attempt is None:
            return
        record = self.attempt.copy()
        terminal = components(
            self.config,
            error_m=None,
            height_enabled=False,
            grasp_reward=int(result == "success"),
            canceled=result == "canceled",
        )
        self.reward = terminal
        valid = terminal["valid"] and record["height_reward_valid"]
        record.update(
            result=result,
            reason=reason,
            terminal_tick=tick,
            terminal_time=now,
            handback_effective_tick=handback_tick,
            grasp_reward=None if result == "canceled" else int(result == "success"),
            height_reward=record["height_reward_sum"] if result != "canceled" else None,
            total_reward=(
                self.config.reward["height_weight"] * record["height_reward_sum"]
                + terminal["total_reward"]
            )
            if valid
            else None,
            reward_valid=valid,
            terminated=result != "canceled",
            truncated=result == "canceled",
            trainable=False,
            exclusion_reasons=["server_audit_required"],
        )
        record["event_id"] = self.event(
            result,
            tick,
            now,
            reason=reason,
            reward=terminal,
            feedback=self.feedback.get(record["arm"]),
        )
        self.emit("attempt", record)
        self.completed.append(record)
        self.phase[record["arm"]] = "WAIT_REARM"
        self.attempt = None
        self.confirm_started = self.confirm_last = None

    def cancel(self, reason, tick, now):
        self._terminate("canceled", reason, tick, now)
        self.previous.clear()

    def reset(self, tick, now):
        self.cancel("reset", tick, now)
        self.phase = dict.fromkeys(ARMS, "READY")
        for arm in ARMS:
            self.mark(arm, eligible=False, empty_hand=False)
        self.event("reset", tick, now)

    def observe(self, *, tick, now, epoch, heights, feedback, policy_active):
        self.event_refs = []
        self.reward = components(self.config, error_m=None)
        self.heights, self.feedback = heights, feedback
        if self.epoch is not None and epoch != self.epoch:
            self.cancel("epoch_changed", tick, now)
        self.epoch = epoch
        if not policy_active:
            self.cancel("control_not_policy", tick, now)
            return
        if self.attempt is not None:
            arm = self.active_arm
            h, options = heights[arm], getattr(self.config, arm)
            if not h.get("height_valid"):
                self.cancel("height_feedback_invalid", tick, now)
                return
            if self.phase[arm] in ("ACTIVE_CLOSURE", "EXIT_PENDING") and not self.fresh_force(
                arm, now
            ):
                self.cancel("force_feedback_invalid_or_stale", tick, now)
                return
            if self.phase[arm] != "EXIT_PENDING" and (
                (
                    options.budget_s is not None
                    and now - self.attempt["entry_time"] >= options.budget_s
                )
                or (
                    options.minimum_height_m is not None
                    and h["height_m"] <= options.minimum_height_m
                )
            ):
                self.exit_pending("failure", "budget_or_height_boundary", tick, now)
            if (
                options.h_goal_m is not None
                and h["height_m"] <= options.h_goal_m
                and not self.attempt.get("goal_reached")
            ):
                self.attempt["goal_reached"] = self.event("height_goal_reached", tick, now)
            if self.phase[arm] == "ACTIVE_CLOSURE":
                f = feedback[arm]
                last = self.last_feedback.get(arm)
                advancing = last is None or f["sdk_updated_at"] > last
                self.last_feedback[arm] = f["sdk_updated_at"]
                # Repeated snapshots cannot extend the confirmation window.
                good = advancing and abs(f["effort_nm"]) > 0.65
                if not good:
                    self.confirm_started = self.confirm_last = None
                elif (
                    self.config.confirm_s is not None
                    and self.config.max_confirmation_gap_s is not None
                ):
                    if (
                        self.confirm_last is None
                        or now - self.confirm_last > self.config.max_confirmation_gap_s
                    ):
                        self.confirm_started = now
                    self.confirm_last = now
                    if now - self.confirm_started >= self.config.confirm_s:
                        self.attempt["reward_proposal_tick"] = tick
                        self.exit_pending("success", "force_confirmed", tick, now)
            if self.phase[arm] == "ACTIVE_DESCENT" and self._reward_tick != (epoch, tick):
                self.reward = components(self.config, error_m=h.get("error_m"))
                self._reward_tick = (epoch, tick)
                self.attempt["height_reward_valid"] &= self.reward["valid"]
                if self.reward["valid"]:
                    self.attempt["height_reward_sum"] += self.reward["height_reward"]
        candidates = []
        for arm in ARMS:
            h, options = heights[arm], getattr(self.config, arm)
            current = h.get("height_m") if h.get("height_valid") else None
            previous = self.previous.get(arm)
            self.previous[arm] = None if current is None else (current, now)
            if self.phase[arm] != "READY" or current is None:
                continue
            if previous is None:
                if current <= options.h_entry_m:
                    self.event("entry_missed", tick, now, missed_arm=arm)
                    self.phase[arm] = "WAIT_REARM"
                continue
            eligible = self.eligibility[arm]
            interval = now - previous[1]
            if (
                interval > 0
                and previous[0] > options.h_entry_m + options.entry_hysteresis_m
                and current <= options.h_entry_m
                and (previous[0] - current) / interval > options.minimum_descent_m_s
                and eligible["eligible"]
                and eligible["empty_hand"]
            ):
                candidates.append(arm)
        if candidates and self.attempt is None:
            arm = self.priority if self.priority in candidates else candidates[0]
            self.priority = "right" if arm == "left" else "left"
            self.attempt = dict(
                attempt_id=uuid.uuid4().hex,
                arm=arm,
                epoch=epoch,
                entry_tick=tick,
                entry_time=now,
                first_residual_tick=None,
                closure_tick=None,
                reward_proposal_tick=None,
                adopted_sources=[],
                config=self.config.as_dict(),
            )
            self.attempt.update(
                height_reward_sum=0.0, height_reward_valid=True, adopted_sources_truncated=0
            )
            self.closure_anchor = (
                None
                if self.previous_command is None
                else self.previous_command["target"][6 if arm == "left" else 13]
            )
            self.reopen_anchor = None
            self.phase[arm] = "ACTIVE_DESCENT"
            self.confirm_started = self.confirm_last = None
            self.event("entry", tick, now, competing_arms=candidates, selected_arm=arm)

    def exit_pending(self, result, reason, tick, now):
        self.phase[self.active_arm] = "EXIT_PENDING"
        self.attempt.update(pending_result=result, pending_reason=reason, exit_requested_tick=tick)
        self.event("handback_requested", tick, now, reason=reason)

    def submitted(self, *, tick, now, target, selection, residual):
        if self.attempt is not None:
            arm = self.active_arm
            index = 6 if arm == "left" else 13
            delta = self.config.close_delta
            if self.phase[arm] == "ACTIVE_DESCENT":
                self.closure_anchor = max(
                    target[index],
                    self.closure_anchor if self.closure_anchor is not None else target[index],
                )
            if (
                delta is not None
                and self.phase[arm] == "ACTIVE_DESCENT"
                and self.closure_anchor - target[index] >= delta
            ):
                self.phase[arm] = "ACTIVE_CLOSURE"
                self.attempt["closure_tick"] = tick
                self.attempt["closure_time"] = now
                self.last_feedback[arm] = (self.feedback.get(arm) or {}).get("sdk_updated_at")
                self.reopen_anchor = target[index]
                self.event("closure_started", tick, now)
            if self.phase[arm] == "ACTIVE_CLOSURE":
                self.reopen_anchor = min(target[index], self.reopen_anchor)
                if delta is not None and target[index] - self.reopen_anchor >= delta:
                    self.exit_pending("failure", "reopened_without_success", tick, now)
            if any(abs(v) > 0 for v in residual) and self.attempt["first_residual_tick"] is None:
                self.attempt["first_residual_tick"] = tick
                self.event("first_residual", tick, now, selection=selection)
            if selection:
                source = dict(selection)
                if (
                    not self.attempt["adopted_sources"]
                    or source != self.attempt["adopted_sources"][-1]
                ):
                    self.attempt["adopted_sources"].append(source)
                    if len(self.attempt["adopted_sources"]) > 64:
                        self.attempt["adopted_sources"].pop(0)
                        self.attempt["adopted_sources_truncated"] += 1
        self.previous_command = dict(tick=tick, time=now, target=list(target), selection=selection)
        if self.attempt is None and self.config.release_position is not None:
            for arm in ARMS:
                h = self.heights.get(arm, {})
                if (
                    self.phase[arm] == "WAIT_REARM"
                    and h.get("height_valid")
                    and h["height_m"] > getattr(self.config, arm).h_entry_m
                    and target[6 if arm == "left" else 13] >= self.config.release_position
                ):
                    self.phase[arm] = "READY"
                    self.previous.pop(arm, None)
                    self.event("rearmed", tick, now, rearmed_arm=arm)

    def handback(self, *, tick, now):
        arm = self.active_arm
        if arm is None or self.phase[arm] != "EXIT_PENDING":
            return False
        result = self.attempt["pending_result"]
        if result == "success" and (
            not self.fresh_force(arm, now) or abs(self.feedback[arm]["effort_nm"]) <= 0.65
        ):
            self.phase[arm] = "ACTIVE_CLOSURE"
            self.confirm_started = self.confirm_last = None
            self.event("success_condition_lost", tick, now)
            return False
        self.event("handback_effective", tick, now)
        self._terminate(result, self.attempt["pending_reason"], tick, now, handback_tick=tick)
        return True
