"""Offline re-evaluation of automatic eligibility from recorded evidence.

Uses captured table-frame FK/SDK samples and scheduling inputs, not a guessed
clock or a neural prediction. This checks the selector, not physical rewards
or the correctness of scene calibration, and never connects hardware.
"""

from .config import ARMS
from .selector import RulesSelector


def verify_selector(rows, config):
    selector = RulesSelector(config, lambda *_: None)
    checked, failures, examples, previous_tick = 0, 0, [], None
    keys = (
        "eligible",
        "empty_hand",
        "selector_state",
        "holding_locked",
        "actual_open_confirmed",
        "closing_detected",
        "closure_tick",
        "closure_time",
        "reason_codes",
        "selector_schema",
        "selector_config_sha",
    )
    for row in rows:
        parts = row.get("parts")
        if not parts:
            continue
        try:
            context, arms = parts["selector_context"], parts["arms"]
            if not context or parts["selector_config_sha"] != config.selector_config_sha:
                raise ValueError("missing_context_or_configuration_mismatch")
            if context.get("initial_state") is not None:
                selector.restore_for_replay(context["initial_state"])
            elif previous_tick is None or row["tick"] != previous_tick + 1:
                raise ValueError("recording_gap_requires_selector_checkpoint")
            snapshots = selector.observe(
                tick=row["tick"],
                now=context["control_time"],
                epoch=context["epoch"],
                policy_active=context["policy_active"],
                heights=arms,
                feedback={a: arms[a]["force_feedback"] for a in ARMS},
                phases=context["phases"],
                active_arm=context["active_arm"],
                committed_arms=context["committed_arms"],
            )
            if context["consumed_arm"] is not None:
                selector.consume(context["consumed_arm"], row["tick"], context["control_time"])
            for arm in ARMS:
                mismatches = [k for k in keys if snapshots[arm][k] != arms[arm].get(k)]
                if mismatches:
                    raise ValueError(f"{arm}:" + ",".join(mismatches))
            selector.submitted(
                tick=row["tick"], now=context["control_time"], target=row["submitted_action"]
            )
            previous_tick = row["tick"]
            checked += 1
        except (KeyError, TypeError, ValueError) as exc:
            failures += 1
            if len(examples) < 16:
                examples.append(dict(tick=row.get("tick"), error=str(exc)))
    return dict(
        valid=bool(checked) and not failures,
        checked_ticks=checked,
        failed_ticks=failures,
        examples=examples,
        scope="recorded_selector_evidence_not_physical_or_training_acceptance",
    )
