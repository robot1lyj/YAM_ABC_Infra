import json
import shutil

import numpy as np
import pytest

from yam_abc_reproduce.hil.batch_convert import convert, discover
from yam_abc_reproduce.hil.storage import SegmentWriter


def episode(path):
    path.mkdir(parents=True)
    writer = SegmentWriter(path, 30, {"station": {"task_name": "sort blocks"}}, min_free_bytes=0)
    image = np.full((32, 32, 3), 90, dtype=np.uint8)
    for tick in range(3):
        writer.append(
            dict(
                tick=tick,
                time=tick / 30,
                epoch=1,
                source="human",
                measured_state=[0.1] * 14,
                observation_state=[0.1] * 14,
                submitted_action=[0.2] * 14,
                observation_valid=True,
                expert_valid=True,
                is_intervention=False,
            ),
            {role: image for role in ("top", "left", "right")},
        )
    writer.close("success")


def test_moved_recordings_convert_without_station_or_job_state(tmp_path):
    local = tmp_path / "local" / "session"
    episode(local / "episode_000001")
    (local / "session.json").write_text(
        json.dumps({"episodes": [{"path": "episode_000001", "outcome": "success"}]})
    )
    server = tmp_path / "server" / "uploaded"
    shutil.copytree(local, server)
    assert list(discover(server)) == [server]
    report = convert(server, tmp_path / "dataset")
    assert report["failed"] == 0
    assert report["datasets"][0]["report"]["frames"] == 3
    assert report["datasets"][0]["report"]["packet_copy_episodes"] == 1
    assert not (server / "lerobot").exists()
    with pytest.raises(FileExistsError):
        convert(server, tmp_path / "dataset")


def test_batch_continues_after_bad_source_and_reports_failure(tmp_path):
    root = tmp_path / "raw"
    episode(root / "a_bad")
    episode(root / "b_good")
    (root / "a_bad" / "segment_000000" / "top.mp4").unlink()
    report = convert(root, tmp_path / "converted")
    assert report["failed"] == 1
    assert [r["state"] for r in report["datasets"]] == ["failed", "complete"]
    assert (tmp_path / "converted/conversion_report.json").exists()


def test_output_cannot_be_nested_in_input(tmp_path):
    episode(tmp_path / "raw")
    with pytest.raises(ValueError, match="独立"):
        convert(tmp_path / "raw", tmp_path / "raw/converted")


def test_capture_cli_does_not_call_converter(tmp_path, monkeypatch):
    from yam_abc_reproduce.hil import lerobot_export, run

    monkeypatch.setattr(
        lerobot_export, "export_session", lambda *a, **kw: pytest.fail("capture invoked conversion")
    )
    output = tmp_path / "session"
    run.main(["--mock", "--demo", "--duration", ".3", "--output", str(output)])
    assert (output / "session.json").exists()
    assert list(output.glob("episode_*/segment_*/samples.h5"))
    assert not (output / "lerobot").exists()
