"""Single-owner tick orchestration. Adapters supply synchronized snapshots.

No hardware is constructed here. The caller applies the complete Decision to
both arms, logs requested and actually submitted commands, and mirrors leaders.
"""

from .core import Arbiter, Phase


class Session:
    def __init__(self, arbiter: Arbiter, worker=None):
        self.arbiter = arbiter
        self.worker = worker

    def tick(
        self,
        state,
        leader,
        *,
        now,
        dt,
        observation_id,
        observation=None,
        fresh=True,
        leader_ready=False,
        event=None,
    ):
        # Local events take precedence over a policy response arriving this tick.
        if event == "start":
            self.arbiter.start(state, leader)
        elif event == "toggle":
            self.arbiter.toggle(state, leader)
        elif event == "stop":
            self.arbiter.fail(state, "operator stop")
        elif event is not None:
            raise ValueError(f"unknown event: {event}")
        if self.worker:
            reply = self.worker.poll()
            if reply is not None and reply.token == self.arbiter.pending:
                if reply.error:
                    self.arbiter.fail(state, reply.error)
                else:
                    try:
                        self.arbiter.accept(reply.token, reply.actions, now)
                    except (ValueError, TypeError):
                        self.arbiter.fail(state, "invalid policy response")
        decision = self.arbiter.step(
            state, leader, now=now, dt=dt, observation_fresh=fresh, leader_ready=leader_ready
        )
        if self.worker and fresh and observation is not None:
            token = self.arbiter.request(observation_id, now)
            if token is not None and not self.worker.submit(token, observation):
                # A stale RPC is still in flight. Retry on a later tick, never wait.
                self.arbiter.pending = None
        return decision

    @property
    def faulted(self):
        return self.arbiter.phase == Phase.FAULT
