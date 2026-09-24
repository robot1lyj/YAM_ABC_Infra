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


def _replay_rows(episode, *, start=0, steps=50):
    episode = Path(episode).resolve()
    if start < 0 or (steps is not None and steps < 1):
        raise ValueError("select a nonnegative start and positive replay frame count")
    manifest = json.loads((episode / "manifest.json").read_text())
    if manifest.get("error") or manifest.get("outcome") not in ("success", "failure", "unknown"):
        raise ValueError("replay requires a completed, non-aborted source episode")
    if manifest.get("fps") != 30:
        raise ValueError("replay requires a 30 Hz source")
    rows = itertools.islice(read_rows(episode), start, None if steps is None else start + steps)
    selected, previous_tick = [], None
    for row in rows:
        if previous_tick is not None and row["tick"] != previous_tick + 1:
            raise ValueError("selected replay has a control tick gap")
        previous_tick = row["tick"]
        selected.append(row)
    if not selected:
        raise ValueError("no frames at selected start")
    return selected


def _submitted_targets(rows):
    targets = np.asarray([row["submitted_action"] for row in rows], dtype=np.float64)
    if targets.shape != (len(targets), 14) or not np.isfinite(targets).all():
        raise ValueError("replay needs finite 14D submitted targets")
    if np.any((targets[:, [6, 13]] < 0) | (targets[:, [6, 13]] > 1)):
        raise ValueError("recorded gripper targets outside [0,1]")
    return targets


def load_targets(episode, *, start=0, steps=50):
    return _submitted_targets(_replay_rows(episode, start=start, steps=steps))


def load_xr1_eef_targets(episode, *, start=0, steps=50, max_joint_error_rad=0.05):
    """Recorded joints -> XR-1 relative TCP deltas -> official bounded IK.

    This is a read-only playback conversion, not model inference. Every 30-row
    window uses its recorded observation as the shared delta origin. The
    original submitted targets are never sent to the playback client.
    """
    from .xr1_actions import ACTION_HORIZON, XR1YamCodec

    if not np.isfinite(max_joint_error_rad) or max_joint_error_rad <= 0:
        raise ValueError("max joint round-trip error must be finite and positive")
    rows = _replay_rows(episode, start=start, steps=steps)
    recorded = _submitted_targets(rows)
    codec = XR1YamCodec()
    converted = np.empty_like(recorded)
    for offset in range(0, len(rows), ACTION_HORIZON):
        window = rows[offset : offset + ACTION_HORIZON]
        observation = np.asarray(window[0].get("observation_state"), dtype=np.float64)
        if observation.shape != (14,) or not np.isfinite(observation).all():
            raise ValueError(f"missing finite observation at replay frame {start + offset}")
        actions = recorded[offset : offset + len(window)]
        try:
            deltas = codec.encode(observation, actions)
            recovered = codec.decode(observation, deltas)
        except ValueError as exc:
            raise ValueError(f"XR-1 conversion failed at replay frame {start + offset}: {exc}") from exc
        difference = float(np.max(np.abs(recovered[:, JOINTS] - actions[:, JOINTS])))
        if difference > max_joint_error_rad:
            raise ValueError(
                f"XR-1 IK changed recorded joint branch at frame {start + offset} "
                f"(max error {difference:.4f} rad)"
            )
        converted[offset : offset + len(window)] = recovered
    return converted


def time_stretch_targets(targets, playback_rate=1.0):
    """Slow a recorded trajectory at the same 30 Hz command cadence.

    ``playback_rate`` is source frames per output frame. Interpolation happens
    only after the complete EEF->IK conversion, so it never changes the IK
    branch or invokes the model. The first and last source targets are kept.
    """
    source = np.asarray(targets, dtype=np.float64)
    if source.ndim != 2 or source.shape[1] != 14 or len(source) < 1 or not np.isfinite(source).all():
        raise ValueError("replay targets must be finite (N,14), N>=1")
    if not np.isfinite(playback_rate) or not 0 < playback_rate <= 1:
        raise ValueError("playback rate must be in (0,1]")
    if playback_rate == 1 or len(source) == 1:
        return source.copy()
    count = int(np.ceil((len(source) - 1) / playback_rate)) + 1
    position = np.minimum(np.arange(count, dtype=np.float64) * playback_rate, len(source) - 1)
    lower = np.floor(position).astype(np.int64)
    upper = np.minimum(lower + 1, len(source) - 1)
    alpha = (position - lower)[:, None]
    result = source[lower] * (1 - alpha) + source[upper] * alpha
    result[0], result[-1] = source[0], source[-1]
    return result


def retime_targets_for_step(targets, max_arm_step_rad):
    """Spend extra 30 Hz ticks on steep replay edges without changing their path.

    Each source interval gets at least one output interval, so no part of the
    recording is sped up. This is confined to recorded replay, after IK.
    """
    source = np.asarray(targets, dtype=np.float64)
    if source.ndim != 2 or source.shape[1] != 14 or len(source) < 1 or not np.isfinite(source).all():
        raise ValueError("replay targets must be finite (N,14), N>=1")
    if not np.isfinite(max_arm_step_rad) or max_arm_step_rad <= 0:
        raise ValueError("max arm replay step must be finite and positive")
    if len(source) == 1:
        return source.copy()
    arm_step = np.max(np.abs(np.diff(source[:, JOINTS], axis=0)), axis=1)
    divisions = np.maximum(1, np.ceil(arm_step / max_arm_step_rad).astype(np.int64))
    result = np.empty((int(divisions.sum()) + 1, 14), dtype=np.float64)
    result[0] = source[0]
    cursor = 1
    for index, count in enumerate(divisions):
        alpha = (np.arange(1, count + 1, dtype=np.float64) / count)[:, None]
        result[cursor : cursor + count] = source[index] * (1 - alpha) + source[index + 1] * alpha
        cursor += count
    return result


class ReplayPolicy:
    def __init__(self, targets, *, start_tolerance_rad=.2, action_representation="joint",
                 playback_rate=1.0, source_frames=None, max_arm_step_rad=None):
        self.targets = np.asarray(targets, dtype=np.float64)
        if not np.isfinite(start_tolerance_rad) or start_tolerance_rad <= 0:
            raise ValueError("start tolerance must be finite and positive")
        self.tolerance = start_tolerance_rad
        self.action_representation = action_representation
        self.playback_rate = playback_rate
        self.source_frames = len(self.targets) if source_frames is None else source_frames
        self.max_arm_step_rad = max_arm_step_rad
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
                "action_representation": self.action_representation,
                "replay_start_frame": start,
                "replay_source_frames": len(self.targets),
                "replay_input_frames": self.source_frames,
                "replay_playback_rate": self.playback_rate,
                "replay_max_arm_step_rad": self.max_arm_step_rad,
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
    parser.add_argument(
        "--xr1-eef-roundtrip", action="store_true",
        help="convert recorded targets through XR-1 EEF deltas and official YAM IK before replay",
    )
    parser.add_argument("--start-tolerance-rad", type=float, default=.2)
    parser.add_argument(
        "--playback-rate", type=float, default=1.0,
        help="source-frame advance per 30 Hz output frame; 0.75 slows replay to 75%%",
    )
    parser.add_argument(
        "--max-arm-step-rad", type=float,
        help="adaptively add 30 Hz replay ticks so each arm joint step stays within this value",
    )
    args = parser.parse_args()
    if args.max_arm_step_rad is not None and args.playback_rate != 1:
        parser.error("select playback rate or max arm step, not both")
    load = load_xr1_eef_targets if args.xr1_eef_roundtrip else load_targets
    source_targets = load(args.episode, start=args.start, steps=None if args.all else args.steps)
    targets = (
        retime_targets_for_step(source_targets, args.max_arm_step_rad)
        if args.max_arm_step_rad is not None
        else time_stretch_targets(source_targets, args.playback_rate)
    )
    representation = "xr1_eef_delta_roundtrip" if args.xr1_eef_roundtrip else "joint"
    from openpi_client import msgpack_numpy
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.server import serve

    def handler(ws):
        replay = ReplayPolicy(
            targets, start_tolerance_rad=args.start_tolerance_rad,
            action_representation=representation,
            playback_rate=args.playback_rate, source_frames=len(source_targets),
            max_arm_step_rad=args.max_arm_step_rad,
        )
        packer = msgpack_numpy.Packer()
        ws.send(packer.pack({
            "source": "recorded_replay", "rtc_mode": "off",
            "action_horizon": 50, "action_dim": 14, "action_dt_s": 1 / 30,
            "episode": str(args.episode.resolve()), "start_frame": args.start,
            "action_representation": representation,
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

    pacing = (f"max arm step {args.max_arm_step_rad:g} rad"
              if args.max_arm_step_rad is not None else f"{args.playback_rate:g}x")
    print(f"Replay only ({representation}): {len(source_targets)} source -> {len(targets)} output "
          f"frames, {pacing}, ws://127.0.0.1:{args.port}; "
          "select sync_hold; EOF holds last target until operator pauses", flush=True)
    with serve(handler, "127.0.0.1", args.port, compression=None,
               max_size=32 * 1024 * 1024) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
