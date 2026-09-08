"""Single-owner tick orchestration. Adapters supply synchronized snapshots.

No hardware is constructed here. The caller applies the complete Decision to
both arms, logs requested and actually submitted commands, and mirrors leaders.
"""

import numpy as np

from .core import Arbiter, Phase


class Session:
    def __init__(self, arbiter: Arbiter, worker=None):
        self.arbiter = arbiter
        self.worker = worker
        self.last_reply = None

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
    ):
        self.last_reply = None
        # Local events take precedence over a policy response arriving this tick.
        if event == "start":
            self.arbiter.start(state, leader)
        elif event == "toggle":
            self.arbiter.toggle(state, leader)
        elif event == "hold":
            self.arbiter.hold(state)
        elif event and event.startswith("mode:"):
            self.arbiter.change_mode(event.split(":", 1)[1], state)
        elif event == "stop":
            self.arbiter.fail(state, "operator stop")
        elif event is not None:
            raise ValueError(f"unknown event: {event}")
        if self.worker:
            reply = self.worker.poll()
            if reply is not None:
                self.last_reply = {
                    "token": reply.token.__dict__,
                    "received_at": now,
                    "discarded": reply.token != self.arbiter.pending,
                    "error": reply.error,
                    "actions": reply.actions
                    if reply.actions is not None and np.isfinite(reply.actions).all()
                    else None,
                }
            if reply is not None and reply.token == self.arbiter.pending:
                if reply.error:
                    self.arbiter.fail(state, reply.error)
                else:
                    try:
                        accepted = self.arbiter.accept(reply.token, reply.actions, now)
                        self.last_reply["discarded"] = not accepted
                    except (ValueError, TypeError):
                        self.last_reply["discarded"] = True
                        self.last_reply["error"] = "invalid policy response"
                        self.arbiter.fail(state, "invalid policy response")
        decision = self.arbiter.step(
            state, leader, now=now, dt=dt, observation_fresh=fresh, leader_ready=leader_ready
        )
        if self.worker and fresh and observation is not None:
            token = self.arbiter.request(observation_id, now, observed_at)
            if token is not None and not self.worker.submit(token, observation):
                # A stale RPC is still in flight. Retry on a later tick, never wait.
                self.arbiter.pending = None
            elif token is not None:
                self.arbiter._last_request_at = now
        return decision

    @property
    def faulted(self):
        return self.arbiter.phase == Phase.FAULT
