"""Single-owner tick orchestration. Adapters supply synchronized snapshots.

No hardware is constructed here. The caller applies the complete Decision to
both arms, logs requested and actually submitted commands, and mirrors leaders.
"""

import numpy as np

from .core import Arbiter, Phase


class Session:
    def __init__(self, arbiter: Arbiter, worker=None, *, rtc_limit_target=None):
        self.arbiter = arbiter
        self.worker = worker
        self.rtc_limit_target = rtc_limit_target
        self.last_reply = None
        self.notice = None
        self.replay_next_frame = 0
        self._replay_block = None

    def submitted(self, decision):
        """Advance replay only after the control owner successfully submits IO."""
        if (self._replay_block is not None and decision.source == "policy"
                and decision.request == self._replay_block[0]
                and decision.action_index is not None):
            self.replay_next_frame = min(
                self._replay_block[1] + decision.action_index + 1, self._replay_block[2],
            )

    def rewind_replay(self):
        self.replay_next_frame = 0
        self._replay_block = None

    def tick(
        self,
        state,
        leader,
        *,
        now,
        dt,
        observation_id,
        observation=None,
        observed_at=None,
        fresh=True,
        leader_ready=False,
        event=None,
        policy_tick=None,
    ):
        self.last_reply = None
        # Local events take precedence over a policy response arriving this tick.
        if event in ("start", "resume_policy"):
            self.notice = None
        if event == "start":
            self.arbiter.start(state, leader)
        elif event == "takeover":
            self.arbiter.takeover(state, leader)
        elif event == "resume_policy":
            self.arbiter.resume_policy(state)
        elif event == "manual_ready":
            self.arbiter.manual_ready(state, leader)
        elif event == "handback_hold":
            self.arbiter.handback_hold(state, leader)
        elif event == "hold":
            self.arbiter.hold(state)
            if self.arbiter.intervention_pending:
                self.arbiter._leader_frozen = np.asarray(leader).copy()
        elif event and event.startswith("mode:"):
            self.arbiter.change_mode(event.split(":", 1)[1], state)
        elif event == "stop":
            self.arbiter.fail(state, "operator stop")
        elif event is not None:
            raise ValueError(f"unknown event: {event}")
        if (
            self.worker is not None
            and not getattr(self.worker, "planner_alive", True)
            and self.arbiter.rtc_timeline is None
            and self.arbiter.phase in (Phase.POLICY, Phase.RESUME)
        ):
            self.arbiter.hold(state)
        if self.worker:
            reply = self.worker.poll()
            if reply is not None:
                self.last_reply = {
                    "token": reply.token.__dict__,
                    "received_at": now,
                    "discarded": reply.token != self.arbiter.pending,
                    "error": reply.error,
                    "worker_elapsed_ms": reply.worker_elapsed_ms,
                    "server_timing": reply.server_timing,
                    "client_timing": reply.client_timing,
                    "actions": reply.actions
                    if reply.actions is not None and np.isfinite(reply.actions).all()
                    else None,
                }
                if reply.token.observation_policy_tick is not None:
                    takeover = (
                        reply.token.observation_policy_tick
                        + reply.token.rtc_delay_steps
                    )
                    self.last_reply.update({
                        "rtc_takeover_tick": takeover,
                        "rtc_reply_tick": policy_tick,
                        "rtc_slack_ticks": takeover - policy_tick,
                    })
            if reply is not None and reply.token == self.arbiter.pending:
                refusal = (reply.server_timing or {}).get("replay_refused")
                if refusal and (reply.server_timing or {}).get("source") == "recorded_replay":
                    self.arbiter.hold(state)
                    self.arbiter._leader_frozen = np.asarray(leader).copy()
                    self.notice = str(refusal)
                    self.last_reply["discarded"] = True
                elif reply.error:
                    if reply.planner_error:
                        self.arbiter.hold(state)
                    else:
                        self.arbiter.fail(state, reply.error)
                else:
                    try:
                        if (reply.server_timing or {}).get("source") == "recorded_replay" and self.arbiter.streaming:
                            raise ValueError("recorded replay requires sync_hold")
                        accepted = (
                            self.arbiter.accept_rtc(
                                reply.token, reply.actions, now, policy_tick,
                                self.rtc_limit_target,
                            ) if self.arbiter.rtc_timeline is not None else
                            self.arbiter.accept_plan(reply.token, reply.plan, now)
                            if self.arbiter.external_planner else
                            self.arbiter.accept(reply.token, reply.actions, now)
                        )
                        self.last_reply["discarded"] = not accepted
                        timing = reply.server_timing or {}
                        if accepted and timing.get("source") == "recorded_replay":
                            self._replay_block = (
                                reply.token, int(timing["replay_start_frame"]),
                                int(timing["replay_source_frames"]),
                            )
                        if accepted and self.arbiter.action_buffer.fusion == "raw":
                            buffer = self.arbiter.action_buffer
                            self.last_reply["trimmed_steps"] = buffer.last_trimmed_steps
                            self.last_reply["new_vs_old_target_joint_max_rad"] = (
                                buffer.last_seam_max_rad
                            )
                            self.last_reply["new_vs_old_target_gripper_max"] = (
                                buffer.last_seam_gripper_max
                            )
                    except (ValueError, TypeError) as exc:
                        self.last_reply["discarded"] = True
                        reason = f"invalid policy response: {exc}"
                        self.last_reply["error"] = reason
                        self.arbiter.fail(state, reason)
        decision = self.arbiter.step(
            state, leader, now=now, dt=dt, observation_fresh=fresh,
            leader_ready=leader_ready, policy_tick=policy_tick,
        )
        if (self.worker and fresh and observation is not None
                and self.arbiter.rtc_timeline is None
                and not (self._replay_block is not None and decision.source == "policy")):
            token = self.arbiter.request(observation_id, now, observed_at)
            request_observation = dict(observation)
            request_observation["_replay_cursor"] = {
                "next_frame": self.replay_next_frame, "epoch": self.arbiter.epoch,
            }
            if token is not None and not self.worker.submit(token, request_observation):
                # A stale RPC is still in flight. Retry on a later tick, never wait.
                self.arbiter.pending = None
            elif token is not None:
                self.arbiter._last_request_at = now
        return decision

    @property
    def faulted(self):
        return self.arbiter.phase == Phase.FAULT
