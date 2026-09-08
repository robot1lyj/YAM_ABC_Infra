import json

import av
import numpy as np
import pyarrow.parquet as pq
import pytest

from yam_abc_reproduce.hil.lerobot_export import export_session
from yam_abc_reproduce.hil.recording import RecordingSession


def make_session(path):
    rec = RecordingSession(
        path, mode="collect", metadata={"mock": True, "station": {"task_name": "test task"}}
    )
    images = {
        role: np.full((32, 32, 3), fill, np.uint8)
        for role, fill in zip(("top", "left", "right"), (30, 90, 180))
    }
    rec.start_episode()
    for i, (source, event) in enumerate(
        zip(
            ("policy", "hold", "human", "hold", "policy"),
            (
                "policy_started",
                "takeover_applied",
                "human_started",
                "resume_requested",
                "policy_started",
            ),
        )
    ):
        row = dict(
            tick=i,
            epoch=i,
            source=source,
            observation_state=[0.1] * 14,
            measured_state=[0.2] * 14,
            leader_state=[0.8] * 14,
            submitted_action=[0.3] * 14,
            is_intervention=source == "human",
            expert_valid=source == "human",
            observation_valid=True,
            transitions=[event],
            intervention_id=int(i > 0),
            time=100 + i / 30,
            event_requested_at=100 + i / 30 - 0.01,
        )
        assert rec.submit(row, images)
    rec.stop_episode("success")
    rec.start_episode()
    rec.submit(row, images)
    rec.stop_episode("discarded")
    rec.close()
    assert not rec.error
    return path


def test_lerobot_keeps_hil_episode_and_signals_and_uses_follower_data(tmp_path):
    source = make_session(tmp_path / "session")
    out = tmp_path / "lerobot"
    report = export_session(source, out)
    assert report["episodes"] == 1 and report["frames"] == 5
    assert report["skipped"] == [{"episode": "episode_000002", "reason": "discarded"}]
    info = json.loads((out / "meta/info.json").read_text())
    assert info["codebase_version"] == "v3.0"
    data = pq.read_table(out / "data/chunk-000/file-000.parquet").to_pydict()
    np.testing.assert_allclose(data["observation.state"], 0.1)
    np.testing.assert_allclose(data["complementary_info.measured_state"], 0.2)
    np.testing.assert_allclose(data["action"], 0.3)
    assert data["complementary_info.event"] == [8, 1, 2, 4, 8]
    assert data["complementary_info.action_source"] == [1, 2, 0, 2, 1]
    assert data["complementary_info.expert_valid"] == [False, False, True, False, False]
    np.testing.assert_allclose(data["timestamp"], np.arange(5) / 30)
    for role, fill in zip(("top", "left", "right"), (30, 90, 180)):
        with av.open(
            str(out / f"videos/observation.images.{role}_rgb/chunk-000/file-000.mp4")
        ) as video:
            frames = list(video.decode(video=0))
            assert len(frames) == 5
            assert abs(frames[0].to_ndarray(format="rgb24").mean() - fill) < 4
    with pytest.raises(FileExistsError):
        export_session(source, out)


def test_lerobot_failure_leaves_partial_not_training_ready_dataset(tmp_path):
    source = make_session(tmp_path / "session")
    (source / "episode_000001/right.mp4").unlink()
    out = tmp_path / "lerobot"
    with pytest.raises(FileNotFoundError):
        export_session(source, out)
    assert not out.exists() and (tmp_path / "lerobot.partial").exists()


def test_performance_window_does_not_grow_with_recording_length():
    from yam_abc_reproduce.hil.metrics import Latencies

    metrics = Latencies(capacity=10)
    for i in range(100):
        report = metrics.add(i / 30, control=0.001, io=0.0002)
    assert all(len(v) == 10 for v in metrics.values.values())
    assert report["control"]["p95_ms"] == 1
