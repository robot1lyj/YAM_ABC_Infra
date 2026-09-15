import json
import threading
import time

import av
import numpy as np
import pyarrow.parquet as pq
import pytest

from yam_abc_reproduce.hil.lerobot_export import export_session
from yam_abc_reproduce.hil.recording import RecordingSession
from yam_abc_reproduce.hil.recovery import recover
from yam_abc_reproduce.hil.storage import ROLES, Samples, SegmentWriter, h5_rows, read_rows
from yam_abc_reproduce.hil.video import PyAvVideo


def row(i):
    return dict(
        tick=i,
        time=10 + i / 30,
        epoch=1,
        source="human",
        is_intervention=True,
        intervention_id=7,
        observation_valid=True,
        expert_valid=True,
        measured_state=[0.1] * 14,
        observation_state=[0.1] * 14,
        submitted_action=[0.2] * 14,
        event_requested_at=None,
        transitions=["human_started"] if i == 0 else [],
        sync={"cameras": {r: {"host_received_at": 10 + i / 30} for r in ROLES}},
    )


def images(i):
    return {r: np.full((32, 32, 3), i * 10, dtype=np.uint8) for r in ROLES}


def make_episode(path, n=7):
    path.mkdir()
    writer = SegmentWriter(
        path,
        30,
        {"station": {"task_name": "sort blocks"}, "mock": True},
        segment_seconds=0.1,
        min_free_bytes=0,
    )
    for i in range(n):
        writer.append(row(i), images(i))
    writer.close("success")
    return path


def test_three_camera_encodes_run_in_parallel(monkeypatch, tmp_path):
    monkeypatch.setenv("YAM_ABC_HIL_VIDEO_ENCODER", "libx264")
    original = PyAvVideo.append
    barrier = threading.Barrier(len(ROLES))

    def synchronized(self, image):
        barrier.wait(timeout=2)
        return original(self, image)

    monkeypatch.setattr(PyAvVideo, "append", synchronized)
    path = tmp_path / "parallel"
    path.mkdir()
    writer = SegmentWriter(path, 30, {}, min_free_bytes=0)
    writer.append(row(0), images(0))
    writer.close("success")
    assert writer.counts == {role: 1 for role in ROLES}


def test_preselected_video_backend_skips_cold_probe(monkeypatch, tmp_path):
    import yam_abc_reproduce.hil.video as video

    def unexpected_probe(*_args, **_kwargs):
        raise AssertionError("video backend was probed during recording")

    monkeypatch.setattr(video, "select_backend", unexpected_probe)
    path = tmp_path / "prewarmed"
    path.mkdir()
    writer = SegmentWriter(path, 30, {}, min_free_bytes=0, video_backend="libx264")
    writer.append(row(0), images(0))
    writer.close("success")
    assert writer.written == 1
    assert writer.metadata["video_encoder"] == "libx264"


def test_segment_boundaries_preserve_one_episode_and_exact_decoded_pixels(tmp_path):
    source = make_episode(tmp_path / "source")
    raw = list(read_rows(source))
    assert [r["tick"] for r in raw] == list(range(7))
    assert [r["video_indices"]["top"] for r in raw] == [0, 1, 2, 0, 1, 2, 0]
    out = tmp_path / "out"
    report = export_session(source, out)
    assert report["episodes"] == 1 and report["frames"] == 7
    table = pq.read_table(out / "data/chunk-000/file-000.parquet").to_pydict()
    assert table["complementary_info.intervention_id"] == [7] * 7
    for role in ROLES:
        original = []
        for p in sorted(source.glob(f"segment_*/{role}.mp4")):
            with av.open(str(p)) as video:
                original.extend(f.to_ndarray(format="rgb24") for f in video.decode(video=0))
        with av.open(
            str(out / f"videos/observation.images.{role}_rgb/chunk-000/file-000.mp4")
        ) as video:
            frames = list(video.decode(video=0))
            assert len(frames) == 7
            assert [float(f.time) for f in frames] == pytest.approx(np.arange(7) / 30, abs=0.001)
            for a, b in zip(original, frames, strict=True):
                np.testing.assert_array_equal(a, b.to_ndarray(format="rgb24"))


def test_hdf5_batches_preserve_nulls_and_ignore_uncommitted_tail(tmp_path):
    path = tmp_path / "samples.h5"
    samples = Samples(path, batch_size=2)
    for i in range(3):
        samples.append(row(i))
    assert len(samples.pending) == 1
    assert len(list(h5_rows(path))) == 2
    samples.close()
    rows = list(h5_rows(path))
    assert len(rows) == 3 and rows[-1]["event_requested_at"] is None
    assert rows[-1]["observation_state"] == [0.1] * 14


def test_session_checkpoints_before_disconnect(tmp_path):
    rec = RecordingSession(tmp_path / "session", mode="collect")
    try:
        rec.start_episode()
        rec.submit(row(0), images(0))
        rec.stop_episode("success")
        deadline = time.monotonic() + 5
        while not rec.episodes and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(rec.episodes) == 1
        assert json.loads((rec.path / "session.json").read_text())["episodes"][0]["steps"] == 1
        assert rec._thread.is_alive()
    finally:
        rec.close()


def test_recovery_keeps_source_and_marks_review(tmp_path):
    source = make_episode(tmp_path / "source")
    # Last segment loses a camera: earlier independently closed segments survive.
    (source / "segment_000002/right.mp4").unlink()
    manifest = (source / "manifest.json").read_bytes()
    report = recover(source, tmp_path / "recovered")
    assert report["frames"] == 6 and report["requires_review"]
    assert (source / "manifest.json").read_bytes() == manifest
    out = export_session(tmp_path / "recovered", tmp_path / "reviewed", allow_recovered=True)
    assert out["frames"] == 6
    assert export_session(tmp_path / "recovered", tmp_path / "unreviewed")["frames"] == 0


def test_expert_selection_does_not_stitch_policy_gap(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    writer = SegmentWriter(
        source, 30, {"station": {"task_name": "sort"}}, segment_seconds=0.1, min_free_bytes=0
    )
    for i in range(7):
        r = row(i)
        if i == 3:
            r.update(source="policy", expert_valid=False)
        writer.append(r, images(i))
    writer.close("success")
    report = export_session(source, tmp_path / "experts", expert_only=True)
    assert report["frames"] == 6 and report["episodes"] == 2


def test_low_disk_is_explicit(tmp_path):
    path = tmp_path / "source"
    path.mkdir()
    writer = SegmentWriter(path, 30, {}, min_free_bytes=2**63)
    with pytest.raises(OSError, match="disk"):
        writer.append(row(0), images(0))
    writer.close("aborted")


def _interrupted_writer(path, ready):
    writer = SegmentWriter(
        path, 30, {"station": {"task_name": "sort"}}, segment_seconds=0.1, min_free_bytes=0
    )
    for i in range(8):
        writer.append(row(i), images(i))
    ready.set()
    time.sleep(30)


def test_actual_process_termination_recovers_closed_segments(tmp_path):
    import multiprocessing as mp

    source = tmp_path / "crash"
    source.mkdir()
    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    process = ctx.Process(target=_interrupted_writer, args=(source, ready))
    process.start()
    try:
        assert ready.wait(5)
    finally:
        process.terminate()
        process.join(5)
    report = recover(source, tmp_path / "recovery")
    assert report["frames"] >= 6
    assert [r["tick"] for r in read_rows(tmp_path / "recovery")][:6] == list(range(6))


def test_legacy_jsonl_episode_still_exports(tmp_path):
    import shutil

    source = make_episode(tmp_path / "v2", n=3)
    legacy = tmp_path / "v1"
    legacy.mkdir()
    manifest = {
        "schema": "yam_hil_v1",
        "fps": 30,
        "steps": 3,
        "outcome": "success",
        "station": {"task_name": "legacy"},
    }
    (legacy / "manifest.json").write_text(json.dumps(manifest))
    rows = list(read_rows(source))
    for r in rows:
        r.pop("_segment", None)
    (legacy / "steps.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    for role in ROLES:
        shutil.copy(source / "segment_000000" / f"{role}.mp4", legacy / f"{role}.mp4")
    assert export_session(legacy, tmp_path / "legacy_export")["frames"] == 3


def test_incomplete_startup_row_still_allows_lossless_packet_copy(tmp_path):
    path = tmp_path / "source"
    path.mkdir()
    writer = SegmentWriter(path, 30, {"station": {"task_name": "test"}}, min_free_bytes=0)
    writer.append(row(0), {})
    writer.append(row(1), images(1))
    writer.append(row(2), images(2))
    writer.close("success")
    report = export_session(path, tmp_path / "export")
    assert report["packet_copy_episodes"] == 1
    assert report["frames"] == 2
