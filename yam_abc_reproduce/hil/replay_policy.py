"""Recorded targets over local OpenPI wire; no robot, cameras or Thor client.

Use the workstation's full-chunk synchronous mode. Each connection starts at
the selected source frame; between chunks the workstation holds while asking
for the next block. At EOF the last target is repeated until operator HOLD.
"""

import argparse
import itertools
import json
from pathlib import Path

import numpy as np

from .storage import read_rows

JOINTS = [0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12]


def load_targets(episode, *, start=0, steps=50):
    episode = Path(episode).resolve()
    if start < 0 or (steps is not None and steps < 1):
        raise ValueError("select a nonnegative start and positive replay frame count")
    manifest = json.loads((episode / "manifest.json").read_text())
    if manifest.get("error") or manifest.get("outcome") not in ("success", "failure", "unknown"):
        raise ValueError("replay requires a completed, non-aborted source episode")
    if manifest.get("fps") != 30:
        raise ValueError("replay requires a 30 Hz source")
    rows = itertools.islice(read_rows(episode), start, None if steps is None else start + steps)
    targets, previous_tick = [], None
    for row in rows:
        if previous_tick is not None and row["tick"] != previous_tick + 1:
            raise ValueError("selected replay has a control tick gap")
        previous_tick = row["tick"]
        targets.append(row["submitted_action"])
    if not targets:
        raise ValueError("no frames at selected start")
    targets = np.asarray(targets, dtype=np.float64)
    if targets.shape != (len(targets), 14) or not np.isfinite(targets).all():
        raise ValueError("replay needs finite 14D submitted targets")
    if np.any((targets[:, [6, 13]] < 0) | (targets[:, [6, 13]] > 1)):
        raise ValueError("recorded gripper targets outside [0,1]")
    return targets


class ReplayPolicy:
    def __init__(self, targets, *, start_tolerance_rad=.2):
        self.targets = np.asarray(targets, dtype=np.float64)
        if not np.isfinite(start_tolerance_rad) or start_tolerance_rad <= 0:
            raise ValueError("start tolerance must be finite and positive")
        self.tolerance = start_tolerance_rad
        self.cursor = 0
        self.epoch = None

    def infer(self, observation):
        if "rtc" in observation:
            raise ValueError("recorded replay requires full-chunk sync_hold, not RTC")
        acknowledgement = observation.get("_replay_cursor")
        if not isinstance(acknowledgement, dict):
            raise ValueError("replay requires execution cursor acknowledgement")
        start, epoch = acknowledgement.get("next_frame"), acknowledgement.get("epoch")
        if type(start) is not int or not 0 <= start <= len(self.targets) or type(epoch) is not int:
            raise ValueError("invalid replay execution cursor")
        if self.epoch != epoch:
            state = np.asarray(observation["observation.state"], dtype=np.float64)
            if state.shape != (14,) or not np.isfinite(state).all():
                raise ValueError("replay needs finite current follower state")
            error = np.max(abs(state[JOINTS] - self.targets[min(start, len(self.targets) - 1), JOINTS]))
            if error > self.tolerance:
                return {
                    "actions": np.tile(state, (50, 1)),
                    "server_timing": {
                        "source": "recorded_replay",
                        "replay_refused": (
                            f"回放保持：当前姿态距第{start}帧最大差{error:.3f}rad，"
                            f"超过{self.tolerance:.3f}rad。请先安全对齐或切换模型来源；未推进回放。"
                        ),
                    },
                }
        self.epoch = epoch
        indices = np.minimum(np.arange(start, start + 50), len(self.targets) - 1)
        self.cursor = start
        return {
            "actions": self.targets[indices].copy(),
            "server_timing": {
                "source": "recorded_replay", "rtc_used": False,
                "replay_start_frame": start,
                "replay_source_frames": len(self.targets),
                "replay_final_block": start + 50 >= len(self.targets),
            },
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episode", type=Path)
    parser.add_argument("--start", type=int, default=0)
    length = parser.add_mutually_exclusive_group()
    length.add_argument("--steps", type=int, default=50)
    length.add_argument("--all", action="store_true", help="replay the entire remaining episode once")
    parser.add_argument("--port", type=int, default=8002)
    parser.add_argument("--start-tolerance-rad", type=float, default=.2)
    args = parser.parse_args()
    targets = load_targets(args.episode, start=args.start, steps=None if args.all else args.steps)
    from openpi_client import msgpack_numpy
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.server import serve

    def handler(ws):
        replay = ReplayPolicy(targets, start_tolerance_rad=args.start_tolerance_rad)
        packer = msgpack_numpy.Packer()
        ws.send(packer.pack({
            "source": "recorded_replay", "rtc_mode": "off",
            "action_horizon": 50, "action_dim": 14, "action_dt_s": 1 / 30,
            "episode": str(args.episode.resolve()), "start_frame": args.start,
        }))
        try:
            for message in ws:
                try:
                    result = replay.infer(msgpack_numpy.unpackb(message))
                except (ValueError, TypeError, KeyError) as exc:
                    ws.send(f"Replay refused: {exc}")
                    break
                ws.send(packer.pack(result))
        except ConnectionClosed:
            pass

    print(f"Replay only: {len(targets)} frames at ws://127.0.0.1:{args.port}; "
          "select sync_hold; EOF holds last target until operator pauses", flush=True)
    with serve(handler, "127.0.0.1", args.port, compression=None,
               max_size=32 * 1024 * 1024) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
