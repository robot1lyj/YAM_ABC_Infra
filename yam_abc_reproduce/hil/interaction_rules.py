"""Replaceable HIL interaction rules; no hardware handles or background threads.

Reloaded only while HOLD, off the control thread; the owner swaps one module
reference at a tick boundary. SDK lifecycle and emergency handling stay fixed.
"""

API_VERSION = 1
HOLD_GAIN = .4
POLICY_GAIN = 1.0  # Native position Kp during HIL policy/replay following only.
ALIGN_TOLERANCE = .05  # Joint radians; handles are not actuated.


def alignment_step(a, leader, dt):
    import numpy as np

    from .core import Phase, vector
    if a.phase != Phase.TAKEOVER or a._alignment is None:
        return
    p = a._alignment
    if p.get("stopped"):
        return
    h = vector(leader)
    joints = [0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12]
    p["error"] = float(np.max(np.abs(h[joints]-p["target"][joints])))
    if p["ready"] and p["error"] <= ALIGN_TOLERANCE:
        return
    p["elapsed"] += dt
    u = min(1., p["elapsed"] / p["duration"])
    a._leader_frozen = p["start"] + (3*u*u - 2*u*u*u)*(p["target"]-p["start"])
    p["error"] = float(np.max(np.abs(h[joints]-p["target"][joints])))
    p["stable"] = p["stable"] + 1 if u == 1 and p["error"] <= ALIGN_TOLERANCE else 0
    p["ready"] = p["stable"] >= 3
    if p["elapsed"] > p["duration"] + 3 and not p["ready"]:
        a._leader_frozen = h.copy()
        p["stopped"] = True
        a.alignment_error = "Leader辅助对齐已停止并保持；可按右①以当前姿态进入相对遥操作。异常阻挡请先检查。"


def button_event(mode, phase, right_edge, primary_edge):
    if mode != "hil":
        return None
    if phase == "takeover" and right_edge and primary_edge:
        return "manual_ready"
    if phase == "human" and primary_edge:
        return "handback_hold"
    return None


def takeover(a, state, leader):
    import numpy as np

    from .core import Mode, Phase, vector
    if a.mode == Mode.HIL and a.phase in (Phase.POLICY, Phase.RESUME):
        a._transition(Phase.TAKEOVER, vector(state))
        a.intervention_pending = True
        a.intervention_waiting = True
        a._leader_frozen = vector(leader)
        start = vector(leader).copy()
        target = vector(state).copy()
        target[[6, 13]] = start[[6, 13]]
        delta = float(np.max(np.abs(target-start)))
        # Cubic ease-in/out, max joint speed .8 rad/s, acceleration 2 rad/s².
        duration = max(.2, 1.5*delta/.8, (6*delta/2)**.5)
        a._alignment = dict(start=start, target=target, duration=duration,
                            elapsed=0., error=delta, stable=0, ready=delta <= ALIGN_TOLERANCE)
        a.alignment_error = None


def manual_ready(a, state, leader):
    from .core import Mode, Phase, vector
    if a.mode != Mode.HIL or a.phase != Phase.TAKEOVER:
        return
    # Capture the actual poses at the button edge. Alignment is assistance,
    # never an unlock gate; transition cancels its trajectory immediately.
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
    for name in ("button_event", "takeover", "manual_ready", "handback_hold", "resume_policy", "alignment_step"):
        if not callable(getattr(module, name, None)):
            raise ValueError(f"missing interaction rule: {name}")
    module.revision = hashlib.sha256(source).hexdigest()[:12]
    return module
