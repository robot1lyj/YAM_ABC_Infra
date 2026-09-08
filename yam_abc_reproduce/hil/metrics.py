"""Bounded, low-frequency summaries of controller latency (not hard real-time claims)."""

from collections import deque

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
