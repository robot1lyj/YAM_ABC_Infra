#!/usr/bin/env bash
set -euo pipefail

project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
i2rt_root="$project_root/third_party/i2rt"
wrap_patch="$project_root/patches/i2rt/0002-exclude-gripper-wrap.patch"
gravity_patch="$project_root/patches/i2rt/0003-single-inverse-dynamics.patch"
align_patch="$project_root/patches/i2rt/0004-align-pinned-gripper-wrap.patch"

if grep -q "def _apply_arm_motor_wrap_offsets" "$i2rt_root/i2rt/robots/get_robot.py"; then
  echo "i2rt linear-gripper wrap safety patch already present"
else
  git -C "$i2rt_root" apply --check "$wrap_patch"
  git -C "$i2rt_root" apply "$wrap_patch"
  echo "Applied official i2rt PR #82 commit e599e9d linear-gripper wrap safety patch"
fi

if grep -q "def _align_gripper_limits_to_motor_position" \
  "$i2rt_root/i2rt/robots/get_robot.py"; then
  echo "i2rt pinned gripper turn-alignment patch already present"
else
  git -C "$i2rt_root" apply --check "$align_patch"
  git -C "$i2rt_root" apply "$align_patch"
  echo "Applied YAM pinned gripper turn-alignment safety patch"
fi

if sed -n '/def _compute_gravity_compensation/,/Server Functions/p' \
  "$i2rt_root/i2rt/robots/motor_chain_robot.py" \
  | grep -q 'return self.kdl.compute_inverse_dynamics'; then
  git -C "$i2rt_root" apply --check "$gravity_patch"
  git -C "$i2rt_root" apply "$gravity_patch"
  echo "Applied official i2rt PR #61 commit 3916586 single inverse-dynamics patch"
else
  echo "i2rt single inverse-dynamics optimization already present"
fi
