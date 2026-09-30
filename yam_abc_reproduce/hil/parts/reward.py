"""Explicit versioned reward recipes; no default research weights."""


def components(config, *, error_m, grasp_reward=None, canceled=False, height_enabled=True):
    recipe = config.reward
    out = dict(
        height_reward=None,
        grasp_reward=None if canceled else grasp_reward,
        total_reward=None,
        valid=False,
        reward_schema=None if recipe is None else recipe.get("schema"),
    )
    if canceled or recipe is None or (height_enabled and error_m is None):
        return out
    if recipe.get("formula") != "negative_absolute_height_error_v1" or not recipe.get("schema"):
        raise ValueError("unregistered PARTS reward formula")
    import math

    weights = [recipe.get("height_weight"), recipe.get("grasp_weight")]
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in weights):
        raise ValueError("PARTS reward weights must be explicitly finite")
    out["height_reward"] = -abs(error_m) if height_enabled else None
    out["total_reward"] = weights[0] * (out["height_reward"] or 0) + weights[1] * (
        grasp_reward or 0
    )
    out["valid"] = True
    return out
