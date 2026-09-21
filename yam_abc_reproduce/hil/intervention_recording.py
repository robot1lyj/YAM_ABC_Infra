"""Omit intervention-entry waiting samples, retain only interval audit metadata."""


class InterventionRecordingGate:
    def __init__(self):
        self.intervals = []
        self.pending = None

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
