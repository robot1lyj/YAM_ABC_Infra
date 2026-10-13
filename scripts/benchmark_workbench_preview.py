"""Offline RTC/recording/preview load comparison; never connects hardware or Thor."""

import argparse
import json
import tempfile
import time
from contextlib import ExitStack
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import av
import numpy as np

from yam_abc_reproduce.hil.run import MockPolicy
from yam_abc_reproduce.hil.storage import episode_segments, read_rows
from yam_abc_reproduce.hil.workbench import Workbench


class DelayedMockPolicy(MockPolicy):
    def __init__(self, *, rtc=False, latency=.195):
        super().__init__(rtc=rtc)
        self.latency = latency

    def infer(self, obs):
        time.sleep(self.latency)
        actions = np.tile(obs["observation.state"], (50, 1))
        actions[:, [0, 7]] += .04 * np.sin(np.arange(50)[:, None] / 15)
        return {"actions": actions}


def check_recording(episode):
    rows = list(read_rows(episode))
    video = {role: 0 for role in ("top", "left", "right")}
    for segment in episode_segments(episode):
        local = [r for r in rows if r["_segment"] == str(segment)]
        for role in video:
            path = segment / f"{role}.mp4"
            expected = sum(role in r.get("video_indices", {}) for r in local)
            with av.open(str(path)) as container:
                frames = sum(1 for _ in container.decode(video=0))
            assert frames == expected, (role, frames, expected)
            video[role] += frames
    replies = {}
    def identity(token):
        return tuple(token[k] for k in ("epoch", "request_id", "observation_id"))
    for row in rows:
        reply = row.get("policy_reply")
        if reply and reply.get("actions") is not None:
            replies[identity(reply["token"])] = np.asarray(reply["actions"])
    maximum, checked = 0., 0
    joints = [0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12]
    for row in rows:
        if row["source"] != "policy":
            continue
        selection = row["policy_selection"]
        assert selection["target_tick"] == row["tick"]
        raw = replies[identity(selection["request"])][selection["model_index"]]
        actual = np.asarray(row["submitted_action"])
        maximum = max(maximum, float(np.max(np.abs(raw[joints] - actual[joints]))))
        checked += 1
    assert maximum < 2e-6, maximum
    return dict(rows=len(rows), video_frames=video, verified_policy_ticks=checked,
                raw_to_submitted_joint_max_rad=maximum if checked else None)


def measure(root, enabled, record_frames, duration, latency):
    service = Workbench(
        SimpleNamespace(
            mode="inference",
            mock=True,
            url=None,
            policy_fusion="rtc",
            station="configs/station_hil.yaml",
            output=root,
            task_root=root / "tasks",
        )
    )
    service.preview_enabled = enabled

    def wait(predicate, seconds=15):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            service.heartbeat()
            time.sleep(0.05)
        assert predicate(), service.status

    overrides = ExitStack()
    overrides.enter_context(patch("yam_abc_reproduce.hil.run.MockPolicy", partial(DelayedMockPolicy, latency=latency)))
    try:
        service.create_task("性能验证", "将积木按颜色分拣", "Sort the LEGO bricks by color.")
        service.connect_cameras()
        wait(lambda: service.camera_state == "connected")
        service.connect()
        wait(lambda: service.runtime is not None and service.runtime.status.get("tick", 0) > 2)
        if not record_frames:
            # Benchmark-only baseline: keep identical RTC and episode boundaries,
            # but omit frame submission/encoding. No production mode is added.
            overrides.enter_context(patch.object(service.runtime.recorder, "submit", return_value=True))
        service.event("start")
        wait(lambda: service.runtime.status.get("phase") == "policy")
        wait(lambda: service.runtime.status.get("recording"))
        before = service.status
        started = time.monotonic()
        while time.monotonic() - started < duration:
            service.heartbeat()
            time.sleep(0.1)
        status = service.status
        actual_duration = time.monotonic() - started
        window = {name: list(values) for name, values in service.runtime.latencies.values.items()}
        percentiles = {name: dict(zip(("p50_ms", "p95_ms", "p99_ms", "max_ms"),
                          np.percentile(values, [50, 95, 99, 100]).tolist(), strict=True))
                       for name, values in window.items()}
        service.event("hold")
        wait(lambda: not service.runtime.status.get("recording"))
        wait(lambda: not service.runtime.recorder.saving, 120)
        output = service.output
        service.disconnect(supported=True)
        wait(lambda: service.thread and not service.thread.is_alive(), 30)
        assert not service.error, service.error
        session = json.loads((output / "session.json").read_text())
        assert len(session["episodes"]) == 1
        episode = output / session["episodes"][0]["path"]
        checked = check_recording(episode)
        return {
            "preview": enabled,
            "record_frames": record_frames,
            "mock_policy_latency_s": latency,
            "duration_s": duration,
            "tick_hz": (status["tick"] - before["tick"]) / actual_duration,
            **checked,
            "performance": percentiles,
            "deadline_misses": status["deadline_misses"] - before["deadline_misses"],
            "rtc_accepted_replies": status["rtc_accepted_replies"],
            "rtc_late_replies": status["rtc_late_replies"],
            "record_metrics": status["record_metrics"],
            "output": str(output),
        }
    finally:
        service.close()
        overrides.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=15)
    parser.add_argument("--policy-latency", type=float, default=.195)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="yam-preview-ab-"))
    if args.duration < 2 or args.policy_latency <= 0 or args.repeats < 1:
        parser.error("duration >= 2, positive policy-latency and repeats required")
    results = []
    for repeat in range(args.repeats):
        for label, preview, frames in (("rtc", False, False), ("record", False, True), ("preview", True, True)):
            result = measure(root / f"{repeat}-{label}", preview, frames, args.duration, args.policy_latency)
            results.append(dict(repeat=repeat, **result))
            print(json.dumps(dict(case=label, repeat=repeat, tick_hz=result["tick_hz"],
                control_work=result["performance"]["control_work"],
                deadline_misses=result["deadline_misses"])), flush=True)
    report = {
        "hardware": "mock, development workstation, synthetic images",
        "limitation": "Not RK3588/D405/CAN/SDK, PLD reply payload or sustained-load evidence; synthetic RGB is easy to compress",
        "runs": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"Report: {args.output}; generated mock datasets: {root}")


if __name__ == "__main__":
    main()
