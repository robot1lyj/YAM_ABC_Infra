"""Replaceable HIL interaction rules; no hardware handles or background threads.

Reloaded only while HOLD, off the control thread; the owner swaps one module
reference at a tick boundary. SDK lifecycle and emergency handling stay fixed.
"""

API_VERSION = 1
HOLD_GAIN = .4
POLICY_GAIN = 1.0  # Native position Kp during HIL policy/replay following only.


def button_event(mode, phase, right_edge, primary_edge):
    if mode != "hil":
        return None
    if phase == "takeover" and right_edge and primary_edge:
        return "manual_ready"
    if phase == "human" and primary_edge:
        return "handback_hold"
    return None


def takeover(a, state, leader):
    from .core import Mode, Phase, vector
    if a.mode == Mode.HIL and a.phase in (Phase.POLICY, Phase.RESUME):
        a._transition(Phase.TAKEOVER, vector(state))
        a.intervention_pending = True
        a.intervention_waiting = True
        a._leader_frozen = vector(leader)


def manual_ready(a, state, leader):
    from .core import Mode, Phase, vector
    if a.mode != Mode.HIL or a.phase != Phase.TAKEOVER:
        return
    q, h = vector(state), vector(leader)
    a._transition(Phase.HUMAN, q)
    a.intervention_waiting = False
    a._offset = q - h
    a._offset[[6, 13]] = 0
    a._pickup = [False, False]
    a._previous_grip = h[[6, 13]].copy()


def handback_hold(a, state, leader):
    from .core import Mode, Phase, vector
    if a.mode == Mode.HIL and a.phase == Phase.HUMAN:
        a._transition(Phase.HOLD, vector(state))
        a._leader_frozen = vector(leader)


def resume_policy(a, state):
    from .core import Mode, Phase
    if a.mode == Mode.HIL and (
        a.phase == Phase.HUMAN or (a.phase in (Phase.HOLD, Phase.TAKEOVER)
                                  and (a.intervention_pending or a._leader_frozen is not None))
    ):
        a._transition(Phase.RESUME, state)
        a.intervention_pending = False
        a.intervention_waiting = False


def load_rules():
    """Compile trusted installed source, not cached bytecode or user input."""
    import hashlib
    import types
    from pathlib import Path

    path = Path(__file__)
    source = path.read_bytes()
    module = types.ModuleType(__name__ + "_candidate")
    module.__package__ = __package__
    module.__file__ = str(path)
    exec(compile(source, str(path), "exec"), module.__dict__)
    if (module.API_VERSION != 1 or not 0 < module.HOLD_GAIN <= 1
            or not 0 < module.POLICY_GAIN <= 1):
        raise ValueError("incompatible interaction rules")
    for name in ("button_event", "takeover", "manual_ready", "handback_hold", "resume_policy"):
        if not callable(getattr(module, name, None)):
            raise ValueError(f"missing interaction rule: {name}")
    module.revision = hashlib.sha256(source).hexdigest()[:12]
    return module
