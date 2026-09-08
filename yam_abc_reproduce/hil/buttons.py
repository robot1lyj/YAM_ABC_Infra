"""Official YAM handles: station-wide primary action and level-triggered hold."""

from .core import Mode, Phase


class HandleButtons:
    def __init__(self, debounce=0.25):
        self.previous = None
        self.last_primary = -float("inf")
        self.debounce = debounce

    def read(self, buttons, *, now, mode, phase):
        # Matrix rows are left/right; a flat pair supports simple mock adapters.
        pairs = [buttons] if buttons and isinstance(buttons[0], bool) else buttons
        current = [(bool(pair[0]), bool(pair[1])) for pair in pairs]
        old = self.previous if self.previous is not None else current
        self.previous = current
        if any(hold for _, hold in current):
            return "hold"
        rising = any(top and not before[0] for (top, _), before in zip(current, old))
        if not rising or now - self.last_primary < self.debounce or phase == Phase.FAULT:
            return None
        self.last_primary = now
        if phase == Phase.HOLD:
            return "start"
        if mode == Mode.COLLECT and phase == Phase.HUMAN:
            return "record"
        if mode == Mode.HIL:
            return "toggle"
        return None
