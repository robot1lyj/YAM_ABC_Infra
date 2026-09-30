"""Strict PARTS handshake and reply validation, independent of model/SDK code."""

import copy

import numpy as np

from .config import ARMS, INDICES, PROTOCOL


def array(value, shape, name):
    out = np.asarray(value)
    if out.shape != shape or out.dtype.kind not in "fiu" or not np.isfinite(out).all():
        raise ValueError(f"PARTS {name} must be finite {shape}")
    return out.copy()


def object_field(value, key):
    result = value.get(key, {})
    if not isinstance(result, dict):
        raise ValueError(f"PARTS {key} must be an object")
    return result


def handshake(metadata, config):
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("PARTS metadata must be an object")
    declaration = (metadata or {}).get("parts")
    if declaration is None:
        if config.mode in ("off", "shadow"):
            return None
        raise ValueError("PARTS unsupported by policy endpoint")
    if not isinstance(declaration, dict):
        raise ValueError("PARTS declaration must be an object")
    if (
        declaration.get("protocol") != PROTOCOL
        or declaration.get("horizon") != 50
        or declaration.get("state_dim") != 14
        or declaration.get("residual_space") != "joint_delta_rad"
        or not np.isclose(declaration.get("action_dt", 0), 1 / 30, rtol=0, atol=1e-8)
        or config.mode not in declaration.get("supported_modes", [])
        or declaration.get("contract_sha") != config.contract_sha
        or declaration.get("feature_schema_id") != config.feature_schema_id
        or declaration.get("behavior_manifest_ref") != config.behavior_manifest_ref
        or declaration.get("selector_schema") != config.selector["schema"]
        or declaration.get("selector_config_sha") != config.selector_config_sha
    ):
        raise ValueError("PARTS handshake contract mismatch")
    for arm in ARMS:
        spec = object_field(object_field(declaration, "per_arm"), arm)
        if spec.get("indices") != list(INDICES[arm]):
            raise ValueError(f"PARTS {arm} joint indices mismatch")
        bound = array(spec.get("B_rad"), (6,), f"{arm}.B_rad")
        expected = getattr(config, arm).B_rad
        if expected is None or np.any(bound < 0) or not np.array_equal(bound, expected):
            raise ValueError(f"PARTS {arm} bounds mismatch")
    return copy.deepcopy(declaration)


def validate_reply(value, request, declaration, config, *, delay_steps=0):
    if declaration is None:
        raise ValueError("PARTS unsupported")
    if not isinstance(value, dict) or any(
        value.get(k) != request.get(k) for k in ("protocol", "contract_sha", "mode", "context")
    ):
        raise ValueError("PARTS reply context/contract mismatch")
    if value.get("behavior_snapshot_id") != config.behavior_snapshot_id:
        raise ValueError("PARTS behavior snapshot mismatch")
    result = copy.deepcopy(value)
    for arm in ARMS:
        candidate = object_field(object_field(result, "candidates"), arm)
        u = array(candidate.get("u"), (50, 6), f"{arm}.u")
        bound = array(candidate.get("B_rad"), (6,), f"{arm}.B_rad")
        mask = np.asarray(candidate.get("editable_mask"))
        if (
            np.any(abs(u) > 1)
            or not np.array_equal(bound, declaration["per_arm"][arm]["B_rad"])
            or mask.shape != (50,)
            or mask.dtype.kind != "b"
            or mask[:delay_steps].any()
            or not candidate.get("actor_snapshot_id")
            or type(candidate.get("exploration_applied")) is not bool
            or (config.mode == "eval" and candidate["exploration_applied"])
        ):
            raise ValueError(f"PARTS invalid {arm} candidate/mask/exploration")
        candidate.update(u=u, B_rad=bound, editable_mask=mask.copy())
    features = object_field(result, "features")
    if features.get("feature_schema_id") != config.feature_schema_id:
        raise ValueError("PARTS feature schema mismatch")
    if "z" in features:
        z = np.asarray(features["z"])
        if (
            list(z.shape) != features.get("shape")
            or str(z.dtype) != features.get("dtype")
            or z.dtype.kind not in "fiu"
            or not np.isfinite(z).all()
        ):
            raise ValueError("PARTS invalid feature array")
    elif config.mode in ("collect", "eval") and (
        not features.get("feature_ref") or not features.get("sha256")
    ):
        raise ValueError("PARTS missing verifiable features")
    return result
