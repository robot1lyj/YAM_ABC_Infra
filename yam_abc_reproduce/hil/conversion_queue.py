"""Durable per-episode jobs. Conversion processes pause while arms are connected."""

import argparse
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from .storage import atomic_json, digest


class ConversionQueue:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lease = (self.root / ".worker.lock").open("a")
        fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.process = None
        self.paused = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.summary = {"queued": 0, "complete": 0, "failed": 0, "state": "idle"}
        self._thread = threading.Thread(target=self._run, daemon=True, name="conversion-jobs")
        self._thread.start()

    def enqueue(self, episode):
        episode = Path(episode).resolve()
        identity = digest(episode / "manifest.json")
        converter = converter_digest()
        job_id = hashlib.sha256((identity + converter).encode()).hexdigest()
        path = self.root / f"{job_id}.json"
        if not path.exists():
            atomic_json(
                path,
                {
                    "source": str(episode),
                    "identity": identity,
                    "converter": converter,
                    "state": "queued",
                    "attempt": 0,
                    "output": None,
                    "error": None,
                },
            )

    def pause(self, paused):
        with self._lock:
            self.paused = paused
            if self.process and self.process.poll() is None:
                try:
                    os.kill(self.process.pid, signal.SIGSTOP if paused else signal.SIGCONT)
                except ProcessLookupError:
                    pass

    def retry(self):
        # Only completed failures; never alter a running job.
        with self._lock:
            for path in self.root.glob("*.json"):
                job = json.loads(path.read_text())
                if job["state"] == "failed":
                    if job.get("converter") != converter_digest():
                        self.enqueue(job["source"])
                        job.update(state="superseded")
                    else:
                        job.update(state="queued", error=None)
                    atomic_json(path, job)

    def _run(self):
        try:
            self._loop()
        except Exception as exc:
            self.summary = dict(self.summary, state="failed", error=str(exc))

    def _loop(self):
        while not self._stop.wait(0.5):
            jobs = [(p, json.loads(p.read_text())) for p in sorted(self.root.glob("*.json"))]
            self.summary = {
                s: sum(j["state"] == s for _, j in jobs) for s in ("queued", "complete", "failed")
            }
            self.summary["state"] = "paused" if self.paused else "idle"
            failures = [j.get("error") for _, j in jobs if j["state"] == "failed"]
            self.summary["error"] = failures[-1] if failures else None
            for path, job in jobs:
                if self._stop.is_set():
                    return
                with self._lock:
                    if self.paused:
                        break
                    if job["state"] not in ("queued", "running"):
                        continue
                    # A previous process interruption is retried to a fresh staging path.
                    job["attempt"] += 1
                    output = (
                        Path(job["source"])
                        / "exports"
                        / f"attempt_{job['attempt']}_{uuid.uuid4().hex[:8]}"
                    )
                    output.parent.mkdir(exist_ok=True)
                    job.update(state="running", output=str(output))
                    atomic_json(path, job)
                    self.process = subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "yam_abc_reproduce.hil.conversion_queue",
                            "--job",
                            str(path),
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    self.summary["state"] = "running"
                while self.process.poll() is None:
                    if self._stop.wait(0.2):
                        with self._lock:
                            try:
                                os.kill(self.process.pid, signal.SIGCONT)
                            except ProcessLookupError:
                                pass
                            self.process.terminate()
                        self.process.wait(timeout=5)
                        return
                # A killed child cannot write a failure receipt itself.
                with self._lock:
                    current = json.loads(path.read_text())
                    if current["state"] == "running":
                        current.update(
                            state="failed", error=f"converter exit {self.process.returncode}"
                        )
                        atomic_json(path, current)
                    self.process = None

    def close(self):
        self._stop.set()
        self._thread.join(8)
        if self._thread.is_alive():
            raise RuntimeError("conversion worker did not stop")
        self._lease.close()


def converter_digest():
    base = Path(__file__).parent
    return hashlib.sha256(
        "".join(
            digest(base / name) for name in ("lerobot_export.py", "storage.py", "video_copy.py")
        ).encode()
    ).hexdigest()


def convert_job(path):
    from .lerobot_export import export_session
    from .recording_process import parent_death_guard

    job = json.loads(path.read_text())
    try:
        parent_death_guard()
        os.nice(10)
        if job["converter"] != converter_digest():
            raise ValueError("converter version changed; enqueue a new versioned job")
        if digest(Path(job["source"]) / "manifest.json") != job["identity"]:
            raise ValueError("source manifest changed since enqueue")
        report = export_session(Path(job["source"]), Path(job["output"]))
        if not report["frames"]:
            raise ValueError("no eligible frames; source requires review")
        job.update(state="complete", report=report, error=None)
    except Exception as exc:
        job.update(state="failed", error=f"{type(exc).__name__}: {exc}")
    atomic_json(path, job)


def main():
    parser = argparse.ArgumentParser(description="处理本地已保存集的 LeRobot 转换队列")
    parser.add_argument("--job", type=Path)
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    if args.job:
        convert_job(args.job)
    elif args.root:
        worker = ConversionQueue(args.root)
        try:
            worker._thread.join()
        except KeyboardInterrupt:
            worker.close()
    else:
        parser.error("需要 --job 或 --root")


if __name__ == "__main__":
    main()
