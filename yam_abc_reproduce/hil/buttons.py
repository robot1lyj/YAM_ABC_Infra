"""Mode-specific official handles. HIL takeover is never a handle event."""

from .core import Mode, Phase


class HandleButtons:
    def __init__(self, debounce=0.25):
        self.previous = None
        self.last = [-float("inf"), -float("inf")]
        self.debounce = debounce

    def read(self, buttons, *, now, mode, phase):
        pairs = [buttons] if buttons and isinstance(buttons[0], bool) else buttons
        current = [(bool(p[0]), bool(p[1])) for p in pairs]
        old = self.previous if self.previous is not None else current
        self.previous = current
        edges = [any(p[i] and not q[i] for p, q in zip(current, old)) for i in (0, 1)]
        pressed = [edge and now - self.last[i] >= self.debounce for i, edge in enumerate(edges)]
        for i, edge in enumerate(edges):
            if edge:
                self.last[i] = now
        if phase == Phase.FAULT:
            return None
        if mode == Mode.COLLECT:
            # A held discard also suppresses a simultaneous/overlapping start.
            if any(p[1] for p in current):
                return "discard" if pressed[1] else None
            if pressed[0] and phase == Phase.HUMAN:
                return "record"
        if mode == Mode.HIL and phase == Phase.HUMAN and pressed[0]:
            return "resume_policy"
        return None
