import json
import threading
import time

import numpy as np

from yam_abc_reproduce.hil.core import Arbiter, Mode, Phase
from yam_abc_reproduce.hil.recording import RecordingSession
from yam_abc_reproduce.hil.storage import read_rows


def test_collection_uses_leader_without_policy_or_intervention():
    q = np.zeros(14)
    a = Arbiter(Mode.COLLECT)
    a.start(q, q)
    d = a.step(q, q, now=1, dt=1 / 30)
    assert d.phase == Phase.HUMAN and d.leader_manual
    assert not d.intervention and d.source == "human"
    assert a.request(1, 1) is None


def test_multiple_manual_episodes_exclude_idle_and_reset_video_indices(tmp_path):
    import av

    rec = RecordingSession(tmp_path / "session", mode="collect")
    im = np.zeros((32, 32, 3), np.uint8)
    assert rec.submit({"tick": 0}, {})
    for episode in range(2):
        rec.start_episode()
        for tick in range(3):
            assert rec.submit(
                {"tick": episode * 10 + tick}, {r: im for r in ("top", "left", "right")}
            )
        rec.stop_episode("success" if episode == 0 else "failure")
        assert rec.submit({"tick": 999}, {})
    rec.close()
    assert not rec.error
    session = json.loads((rec.path / "session.json").read_text())
    assert [e["outcome"] for e in session["episodes"]] == ["success", "failure"]
    for e in session["episodes"]:
        path = rec.path / e["path"]
        rows = list(read_rows(path))
        assert len(rows) == 3 and all(r["tick"] != 999 for r in rows)
        assert [r["video_indices"]["top"] for r in rows] == [0, 1, 2]
        for role in ("top", "left", "right"):
            with av.open(str(path / "segment_000000" / f"{role}.mp4")) as video:
                assert len(list(video.decode(video=0))) == 3


def test_idle_collection_creates_no_episode_and_mode_boundaries_are_ordered(tmp_path):
    rec = RecordingSession(tmp_path / "empty", mode="collect")
    rec.close()
    assert not list(rec.path.glob("episode_*"))
    rec = RecordingSession(tmp_path / "switch", mode="hil")
    assert not rec.recording
    rec.start_episode()
    assert rec.submit({"mode": "hil"}, {})
    rec.set_mode("collect")
    assert rec.submit({"mode": "idle"}, {})
    rec.start_episode()
    assert rec.submit({"mode": "collect"}, {})
    rec.set_mode("teleop", "success")
    assert not rec.recording
    rec.start_episode()
    assert rec.submit({"mode": "teleop"}, {})
    rec.close()
    rows = [next(read_rows(p)) for p in sorted(rec.path.glob("episode_*"))]
    assert [r["mode"] for r in rows] == ["hil", "collect", "teleop"]


def test_episode_writer_backpressure_never_blocks_control(tmp_path, monkeypatch):
    ready = threading.Event()
    run = RecordingSession._run

    def delayed(self):
        ready.wait(2)
        run(self)

    monkeypatch.setattr(RecordingSession, "_run", delayed)
    rec = RecordingSession(tmp_path / "session", mode="collect", capacity=1)
    try:
        rec.start_episode()
        start = time.monotonic()
        assert not rec.submit({"tick": 0}, {})
        assert time.monotonic() - start < 0.1
        assert rec.error
    finally:
        ready.set()
        rec.close("aborted")


def test_handle_buttons_independent_edges_and_no_hil_takeover():
    from yam_abc_reproduce.hil.buttons import HandleButtons

    b = HandleButtons()

    def read(keys, t, mode=Mode.HIL, phase=Phase.HUMAN):
        return b.read(keys, now=t, mode=mode, phase=phase)

    assert read([[True, False], [False, False]], 0) is None
    read([[False, False], [False, False]], 0.3)
    assert read([[True, False], [False, False]], 0.6) == "resume_policy"
    assert read([[True, False], [True, False]], 1) == "resume_policy"
    assert read([[False, True], [False, False]], 1.3) is None
    assert read([[True, True], [False, False]], 1.6, phase=Phase.POLICY) is None


def test_handle_primary_action_per_mode_and_discard_priority():
    from yam_abc_reproduce.hil.buttons import HandleButtons

    for mode in Mode:
        b = HandleButtons()
        b.read([[False, False]] * 2, now=0, mode=mode, phase=Phase.HOLD)
        assert b.read([[True, False]] * 2, now=1, mode=mode, phase=Phase.HOLD) is None
        b.read([[False, False]] * 2, now=2, mode=mode, phase=Phase.HUMAN)
        expected = (
            "record" if mode == Mode.COLLECT else "resume_policy" if mode == Mode.HIL else None
        )
        assert b.read([[True, False]] * 2, now=3, mode=mode, phase=Phase.HUMAN) == expected
        b.read([[False, False]] * 2, now=4, mode=mode, phase=Phase.HUMAN)
        expected = (
            "discard" if mode == Mode.COLLECT else "resume_policy" if mode == Mode.HIL else None
        )
        assert b.read([[True, True]] * 2, now=5, mode=mode, phase=Phase.HUMAN) == expected


def test_runtime_collection_buttons_and_hold_cancel_pending_start(tmp_path):
    from yam_abc_reproduce.camera.mock_camera import MockCamera
    from yam_abc_reproduce.camera.worker import CameraWorker
    from yam_abc_reproduce.config import StationConfig
    from yam_abc_reproduce.hil.export import export
    from yam_abc_reproduce.hil.run import Runtime
    from yam_abc_reproduce.hil.station import StationIO
    from yam_abc_reproduce.runtime import build_arm_units

    io = StationIO(build_arm_units(StationConfig(), mock=True), mock=True)
    keys = [[False, False], [False, False]]
    original_read = io.read

    def read():
        q, h, _, ages = original_read()
        return q, h, [p.copy() for p in keys], ages

    io.read = read
    cameras = [
        CameraWorker(MockCamera(r, r, width=32, height=32)) for r in ("top", "left", "right")
    ]
    rec = RecordingSession(
        tmp_path / "session",
        mode="collect",
        metadata={"mock": True, "station": {"task_name": "test"}},
    )
    runtime = Runtime(io, cameras, None, rec, mode="collect")
    thread = threading.Thread(target=runtime.run, kwargs={"duration": 8})

    def wait(predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert predicate(), runtime.status

    try:
        for c in cameras:
            c.start()
        thread.start()
        wait(lambda: runtime.status["tick"] > 2)
        runtime.event("start")
        wait(lambda: runtime.status["phase"] == "human")
        for _ in range(2):
            runtime.event("record")
            wait(lambda: runtime.status.get("recording"))
            tick = runtime.status["tick"]
            wait(lambda: runtime.status["tick"] >= tick + 4)
            runtime.event("success")
            runtime.event("record")
            wait(lambda: not runtime.status.get("recording"))
        runtime.event("record")
        wait(lambda: runtime.status.get("recording"))
        keys[1][1] = True
        wait(lambda: not runtime.status["recording"])
        assert runtime.status["phase"] == "human"
        tick = runtime.status["tick"]
        wait(lambda: runtime.status["tick"] >= tick + 3)
        assert runtime.status["phase"] == "human"
        keys[1][1] = False
        runtime.event("start")
        runtime.event("start")
        runtime.event("hold")
        wait(lambda: runtime.status["phase"] == "hold")
        tick = runtime.status["tick"]
        wait(lambda: runtime.status["tick"] >= tick + 3)
        assert runtime.status["phase"] == "hold"
        runtime.event("quit")
        thread.join(2)
        rec.close(runtime.outcome)
        assert not rec.error
        manifests = [
            json.loads(p.read_text()) for p in sorted(rec.path.glob("episode_*/manifest.json"))
        ]
        assert [m["outcome"] for m in manifests] == ["success", "success"]
        index = json.loads((rec.path / "session.json").read_text())
        assert index["episodes"][-1]["outcome"] == "discarded"
        assert not (rec.path / index["episodes"][-1]["path"]).exists()
        rows = [row for row in read_rows(rec.path / "episode_000001")]
        assert all(r["mode"] == "collect" and not r["is_intervention"] for r in rows)
        assert all(r["expert_valid"] for r in rows)
        export(rec.path / "episode_000001", tmp_path / "expert")
    finally:
        runtime.event("quit")
        thread.join(2)
        rec.close("aborted") if rec._thread.is_alive() else None
        for c in cameras:
            c.stop()
        io.close()


def test_session_manifest_failure_is_reported(tmp_path, monkeypatch):

    from yam_abc_reproduce.hil import storage

    original = storage.atomic_json

    def write(path, *args, **kwargs):
        if path.name == "session.json":
            raise OSError("disk full")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(storage, "atomic_json", write)
    rec = RecordingSession(tmp_path / "session", mode="collect")
    rec.close()
    assert "disk full" in rec.error


def test_failed_episode_is_listed_as_aborted(tmp_path, monkeypatch):
    from yam_abc_reproduce.hil.recording import Recorder

    def fail(self):
        self.error = "encoder failure"

    monkeypatch.setattr(Recorder, "_run", fail)
    rec = RecordingSession(tmp_path / "session", mode="collect")
    rec.start_episode()
    rec.submit({"tick": 1}, {})
    rec.close()
    manifest = json.loads((rec.path / "session.json").read_text())
    assert "encoder failure" in manifest["error"]
    assert manifest["episodes"][0]["outcome"] == "aborted"


def test_default_session_queue_covers_encoder_spawn_burst(tmp_path):
    rec = RecordingSession(tmp_path / "startup", fps=30)
    try:
        assert rec.queue.maxsize == 300
    finally:
        rec.close("aborted")


def test_workstation_closes_arm_units_once(monkeypatch, tmp_path):
    from yam_abc_reproduce.hil import run
    from yam_abc_reproduce.hil.station import StationIO

    original_close = StationIO.close
    calls = []

    def close_once(self):
        calls.append(True)
        return original_close(self)

    monkeypatch.setattr(StationIO, "close", close_once)
    run.main(
        [
            "--mock",
            "--mode",
            "collect",
            "--duration",
            "0.1",
            "--output",
            str(tmp_path / "close_once"),
        ]
    )
    assert len(calls) == 1
