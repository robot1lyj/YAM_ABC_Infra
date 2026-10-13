"""Bounded, low-frequency summaries of controller latency (not hard real-time claims)."""

from collections import deque
from time import monotonic

import numpy as np


class Latencies:
    def __init__(self, capacity=300):
        self.values = {}
        self.cached = {}
        self.next_report = 0

        self.capacity = capacity

    def add(self, now, **seconds):
        for name, value in seconds.items():
            self.values.setdefault(name, deque(maxlen=self.capacity)).append(value * 1000)
        if now >= self.next_report:
            self.cached = {
                name: dict(
                    zip(
                        ("p50_ms", "p95_ms", "p99_ms", "max_ms"),
                        np.percentile(values, [50, 95, 99, 100]).tolist(),
                    )
                )
                for name, values in self.values.items()
            }
            self.next_report = now + 1
        return self.cached

    def finish_tick(self, started_at, **seconds):
        """Include status construction and these statistics in total work time.

        Total-work percentiles summarize completed earlier ticks (at most one
        report period behind), not a partial tick that excludes its own overhead.
        """
        report = self.add(started_at, **seconds)
        self.values.setdefault("control_work", deque(maxlen=self.capacity)).append(
            (monotonic() - started_at) * 1000
        )
        return report
