"""Offline A/B capture benchmark. Uses mock hardware only, no network or motor IO."""

import argparse
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import av

from yam_abc_reproduce.hil.workbench import Workbench


def measure(root, enabled, duration):
    service = Workbench(
        SimpleNamespace(
            mode="collect",
            mock=True,
            url=None,
            station="configs/station_hil.yaml",
            output=root,
            task_root=root / ("tasks-preview-on" if enabled else "tasks-preview-off"),
            baseline=False,
            raw_only=True,
        )
    )
    service.preview_enabled = enabled

    def wait(predicate, seconds=15):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            service.heartbeat()
            time.sleep(0.05)
        assert predicate(), service.status

    try:
        service.create_task("性能验证", "将积木按颜色分拣", "Sort the LEGO bricks by color.")
        service.connect_cameras()
        wait(lambda: service.camera_state == "connected")
        service.connect()
        wait(lambda: service.runtime is not None and service.runtime.status.get("tick", 0) > 2)
        service.event("start")
        wait(lambda: service.runtime.status.get("phase") == "human")
        service.event("record")
        wait(lambda: service.runtime.status.get("recording"))
        started = time.monotonic()
        while time.monotonic() - started < duration:
            service.heartbeat()
            time.sleep(0.1)
        status = service.status
        service.event("record")
        wait(lambda: not service.runtime.status.get("recording"))
        output = service.output
        service.disconnect(supported=True)
        wait(lambda: service.thread and not service.thread.is_alive(), 30)
        assert not service.error, service.error
        session = json.loads((output / "session.json").read_text())
        assert len(session["episodes"]) == 1
        episode = output / session["episodes"][0]["path"]
        rows = [json.loads(line) for line in (episode / "steps.jsonl").read_text().splitlines()]
        video = {}
        for role in ("top", "left", "right"):
            with av.open(str(episode / f"{role}.mp4")) as container:
                video[role] = sum(1 for _ in container.decode(video=0))
            expected = sum(role in r.get("video_indices", {}) for r in rows)
            assert video[role] == expected, (role, video[role], expected)
        return {
            "preview": enabled,
            "duration_s": duration,
            "rows": len(rows),
            "video_frames": video,
            "control_work": status["performance"]["control_work"],
            "deadline_misses": status["deadline_misses"],
            "record_metrics": status["record_metrics"],
            "output": str(output),
        }
    finally:
        service.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=15)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="yam-preview-ab-"))
    results = [measure(root, enabled, args.duration) for enabled in (False, True)]
    report = {
        "hardware": "mock, development workstation, synthetic images",
        "limitation": "Not RK3588/D405 or a sustained load guarantee",
        "runs": results,
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
