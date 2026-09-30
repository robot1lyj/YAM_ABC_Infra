"""Data-only RL run replacement through the existing paused control barrier."""

import threading
from dataclasses import dataclass, field


@dataclass
class RunReplacement:
    previous: object
    client: object
    journal: object
    done: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    accepted: bool = False
    canceled: bool = False
    error: str | None = None

    def apply(self, runtime):
        # The control owner swaps references only; joining the writer is external.
        with self.lock:
            if (
                self.canceled
                or not runtime.task_switching
                or runtime.parts is not self.previous
                or runtime.session.arbiter.phase.value != "hold"
            ):
                self.error = "RL 数据会话状态已变化，未切换"
            else:
                self.previous.machine.cancel(
                    "data_session_changed", self.previous.tick, self.previous.now
                )
                self.previous.cancel_pending("data_session_changed")
                runtime.parts, runtime.parts_journal = self.client, self.journal
                runtime.session.parts = self.client
                runtime.recorder.metadata["parts"] = dict(
                    schema="yam_parts_raw_v1",
                    run_id=self.client.run_id,
                    contract_sha=self.client.config.contract_sha,
                    mode=self.client.config.mode,
                    run_path=str(self.journal.path),
                )
                self.accepted = True
            self.done.set()

    def install(self, runtime):
        previous_journal = runtime.parts_journal
        runtime.task_releases.put(("parts_replace", self))
        self.done.wait(2)
        with self.lock:
            if not self.accepted:
                self.canceled = True  # A late tick must not install a closed journal.
        if self.accepted:
            previous_journal.close()
        else:
            self.journal.close()
            raise ValueError(self.error or "控制线程未确认 RL 数据会话切换；机械臂未被重连")
