#!/usr/bin/env python3
"""Replay one NPZ observation against Thor; no motors and no RTC.

NPZ keys: observation.state, observation.images.top_rgb/left_rgb/right_rgb.
This is an integration probe, not a robot rollout or a Kai0-derived implementation.
"""

import argparse
import json
import time
from pathlib import Path

import numpy as np

from yam_abc_reproduce.hil.core import vector
from yam_abc_reproduce.hil.policy import PlainPolicyClient


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", required=True)
    p.add_argument("--observation", type=Path, required=True)
    p.add_argument("--prompt", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--timeout", type=float, default=2.0)
    args = p.parse_args()
    if args.output.exists():
        p.error("output already exists; choose a new path")
    with np.load(args.observation, allow_pickle=False) as sample:
        obs = {"observation.state": vector(sample["observation.state"]), "prompt": args.prompt}
        for role in ("top", "left", "right"):
            key = f"observation.images.{role}_rgb"
            im = sample[key]
            if im.ndim != 3 or im.shape[-1] != 3 or im.dtype != np.uint8:
                p.error(f"{key} must be HWC uint8 RGB")
            obs[key] = im.copy()
    client = PlainPolicyClient(args.url, timeout=args.timeout)
    try:
        start = time.monotonic()
        out = client.infer(obs)
        elapsed = time.monotonic() - start
        actions = np.asarray(out["actions"])
        if actions.shape != (50, 14) or not np.isfinite(actions).all():
            raise ValueError("expected finite (50,14) actions")
        for row in actions:
            vector(row)
        args.output.mkdir(parents=True, exist_ok=False)
        np.save(args.output / "actions.npy", actions)
        report = {
            "elapsed_s": elapsed,
            "metadata": client.metadata,
            "shape": list(actions.shape),
            "rtc": False,
            "scope": "single network replay; no robot, units/norm/quality not certified",
        }
        (args.output / "report.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
        print(json.dumps(report, default=str))
    finally:
        client.close()


if __name__ == "__main__":
    main()
