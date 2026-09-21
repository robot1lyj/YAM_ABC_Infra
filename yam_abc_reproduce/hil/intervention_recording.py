"""Recorder-owned HIL wait removal, compact timeline and per-episode audit."""


class InterventionRecordingGate:
    def __init__(self):
        self.intervals = []
        self.pending = None
        self.frame_index = 0

    def process(self, row, fps):
        """Recorder-owned selection; original clocks and vectors stay untouched."""
        phase = row.get("phase")
        reason = "handback_wait" if phase == "hold" else "takeover_wait"
        waiting = row.get("mode") == "hil" and (
            phase == "takeover" or (phase == "hold" and row.get("intervention_pending", False)))
        kept = self.filter(row, waiting, reason)
        if kept is None:
            return None
        kept = dict(kept, frame_index=self.frame_index, timestamp=self.frame_index / fps,
                    wait_boundary="omitted_intervention_wait" in kept)
        self.frame_index += 1
        return kept

    def filter(self, row, waiting, reason="takeover_wait"):
        if waiting:
            if self.pending is None or self.pending["reason"] != reason:
                self.pending = dict(intervention_id=row["intervention_id"],
                                    reason=reason,
                                    first_tick=row["tick"], start_time=row["time"],
                                    event_requested_at=row.get("event_requested_at"),
                                    event_applied_at=row.get("event_applied_at"), frames=0)
                self.intervals.append(self.pending)
            self.pending.update(last_tick=row["tick"], end_time=row["time"],
                                frames=self.pending["frames"]+1)
            return None
        if self.pending is not None:
            row = dict(row, omitted_intervention_wait=dict(self.pending))
            self.pending = None
        return row

    def audit(self):
        return [dict(interval) for interval in self.intervals]


def load_recording_gate():
    """Load trusted local rules once per episode, only in the recorder owner."""
    import hashlib
    from pathlib import Path
    path = Path(__file__)
    source = path.read_bytes()
    namespace = {"__file__": str(path), "__name__": __name__}
    exec(compile(source, str(path), "exec"), namespace)
    gate = namespace["InterventionRecordingGate"]()
    gate.revision = hashlib.sha256(source).hexdigest()[:12]
    return gate
