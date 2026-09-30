"""Official grasp-site FK with explicit base-Z or calibrated-table semantics."""

import numpy as np

from .config import ARMS


def validate_table(table, arm):
    """Pure configuration validation, also called before constructing devices."""
    if not isinstance(table, dict):
        raise ValueError(f"invalid PARTS table calibration for {arm}")
    transform = np.asarray(table.get("base_to_table"), dtype=float)
    normal = np.asarray(table.get("normal"), dtype=float)
    origin = np.asarray(table.get("point"), dtype=float)
    if (
        transform.shape != (4, 4)
        or normal.shape != (3,)
        or origin.shape != (3,)
        or not all(np.isfinite(v).all() for v in (transform, normal, origin))
        or not np.allclose(transform[3], [0, 0, 0, 1])
        or not np.allclose(transform[:3, :3].T @ transform[:3, :3], np.eye(3), atol=1e-6)
        or not np.isclose(np.linalg.det(transform[:3, :3]), 1, atol=1e-6)
        or not np.isclose(np.linalg.norm(normal), 1, atol=1e-6)
        or not table.get("frame")
        or not table.get("calibration_id")
    ):
        raise ValueError(f"invalid PARTS table calibration for {arm}")
    return transform, normal, origin


class Heights:
    def __init__(self, config, kinematics=None):
        if kinematics is None:
            from ..kinematics import DualArmEefConverter

            kinematics = DualArmEefConverter()
        self.kinematics, self.config = kinematics, config
        self.tables = {}
        for arm in ARMS:
            table = getattr(config, arm).table
            if table is None:
                continue
            self.tables[arm] = validate_table(table, arm)

    def sample(self, state, *, sampled_at, feedback_age_s, now):
        q = np.asarray(state, dtype=float)
        if q.shape != (14,) or not np.isfinite(q).all():
            raise ValueError("PARTS FK requires finite 14D state")
        eef = self.kinematics.forward(q)
        result = {}
        for i, arm in enumerate(ARMS):
            pose = np.asarray(getattr(eef, arm))
            age = feedback_age_s[i]
            valid = (
                pose.shape == (4, 4)
                and np.isfinite(pose).all()
                and age is not None
                and np.isfinite(age)
                and age >= 0
                and self.config.max_pose_age_s is not None
                and age + max(0, now - sampled_at) <= self.config.max_pose_age_s
            )
            height = None
            position = None
            options = getattr(self.config, arm)
            if valid and options.height_reference == "base_z":
                height = float(pose[2, 3])
            elif valid and arm in self.tables:
                transform, normal, origin = self.tables[arm]
                position = (transform @ pose)[:3, 3]
                height = float(np.dot(normal, position - origin))
            result[arm] = dict(
                position=pose[:3, 3].tolist(),
                orientation=pose[:3, :3].tolist(),
                pose_frame=f"{arm}_base",
                pose_ref="linear_4310/grasp_site",
                pose_sampled_at=sampled_at,
                pose_valid=bool(valid),
                feedback_age_s=age,
                height_m=height,
                height_valid=height is not None,
                height_reference=options.height_reference,
                height_frame=f"{arm}_base" if options.height_reference == "base_z"
                else options.table["frame"] if arm in self.tables else None,
                table_position_m=None if position is None else position.tolist(),
                table_frame=None if arm not in self.tables else options.table["frame"],
                table_calibration_id=None
                if arm not in self.tables
                else options.table["calibration_id"],
                h_entry_m=options.h_entry_m,
                h_goal_m=options.h_goal_m,
                error_m=None
                if height is None or options.h_goal_m is None
                else height - options.h_goal_m,
                table=options.table,
                fk_model="i2rt/YAM/linear_4310",
                units="m",
            )
        return result
