import hashlib
import json
import pickle
import time

import av
import numpy as np
import pytest

from yam_abc_reproduce.hil.recording_process import EncoderProcess
from yam_abc_reproduce.hil.recovery import recover
from yam_abc_reproduce.hil.storage import ROLES, SegmentWriter, read_rows


def sample(index):
    row = dict(tick=index, time=100 + index / 30, frame_index=index, epoch=1,
               submitted_action=np.full(14, index / 100))
    images = {role: np.full((32, 32, 3), index * 10, dtype=np.uint8) for role in ROLES}
    return row, images


def episode(path, count):
    path.mkdir()
    writer = SegmentWriter(path, 30, {"mock": True}, segment_seconds=.1,
                           min_free_bytes=0, video_backend="libx264")
    for index in range(count):
        writer.append(*sample(index))
    writer.close("aborted")
    return path


def spool(path, indices):
    directory = path / ".recording-spool"
    directory.mkdir(exist_ok=True)
    for index in indices:
        (directory / f"{index:012d}.pkl").write_bytes(pickle.dumps(sample(index), protocol=5))
    return directory


def fingerprint(path):
    return {str(file.relative_to(path)): hashlib.sha256(file.read_bytes()).hexdigest()
            for file in path.rglob("*") if file.is_file()}


def assert_frames(path, expected):
    rows = list(read_rows(path))
    assert [row["tick"] for row in rows] == expected
    for row in rows:
        np.testing.assert_allclose(row["submitted_action"], row["tick"] / 100)
    manifest = json.loads((path / "manifest.json").read_text())
    assert manifest["outcome"] == "recovered"
    assert manifest["recovery"]["requires_review"]
    for entry in manifest["segments"]:
        for role in ROLES:
            with av.open(str(path / entry["path"] / f"{role}.mp4")) as video:
                assert sum(1 for _ in video.decode(video=0)) == entry["steps"]


def test_spool_only_episode_recovers_and_preserves_source(tmp_path):
    source = episode(tmp_path / "source", 0)
    spool(source, range(4))
    before = fingerprint(source)
    output = tmp_path / "output"
    result = recover(source, output)
    assert result["frames"] == result["spool"]["recovered_frames"] == 4
    assert result["errors"] == []
    assert_frames(output, list(range(4)))
    assert fingerprint(source) == before


def test_spool_overlap_deduplicates_original_frame_identity(tmp_path):
    source = episode(tmp_path / "source", 6)
    spool(source, range(3, 9))
    before = fingerprint(source)
    output = tmp_path / "output"
    result = recover(source, output)
    assert result["frames"] == 9
    assert result["spool"]["recovered_frames"] == 3
    assert result["spool"]["duplicate_frames"] == 3
    assert_frames(output, list(range(9)))
    assert fingerprint(source) == before


def test_spool_restores_broken_middle_segment_before_later_frames(tmp_path):
    source = episode(tmp_path / "source", 9)
    (source / "segment_000001" / "right.mp4").write_bytes(b"broken video")
    spool(source, range(3, 9))
    before = fingerprint(source)
    output = tmp_path / "output"
    result = recover(source, output)
    assert result["frames"] == 9
    assert result["spool"]["recovered_frames"] == 3
    assert result["spool"]["duplicate_frames"] == 3
    assert result["errors"][0]["segment"] == "segment_000001"
    assert_frames(output, list(range(9)))
    assert fingerprint(source) == before


def test_broken_and_incomplete_spool_files_are_reported_without_losing_later_frames(tmp_path):
    source = episode(tmp_path / "source", 0)
    directory = spool(source, (0, 4))
    (directory / "000000000001.pkl").write_bytes(b"not a pickle")
    row, images = sample(2)
    images.pop("right")
    (directory / "000000000002.pkl").write_bytes(pickle.dumps((row, images)))
    (directory / "000000000003.tmp").write_bytes(b"unfinished")
    before = fingerprint(source)
    output = tmp_path / "output"
    result = recover(source, output)
    assert result["spool"] == dict(files=5, recovered_frames=2, duplicate_frames=0,
                                   unrecoverable_files=3)
    assert len(result["errors"]) == 3
    assert_frames(output, [0, 4])
    assert fingerprint(source) == before


def test_spool_cannot_execute_pickled_code(tmp_path):
    source = episode(tmp_path / "source", 0)
    directory = spool(source, ())
    (directory / "000000000000.pkl").write_bytes(b"cos\nsystem\n(S'false'\ntR.")
    result = recover(source, tmp_path / "output")
    assert result["frames"] == 0 and result["spool"]["unrecoverable_files"] == 1
    assert "unsupported spool object" in result["errors"][0]["error"]


@pytest.mark.parametrize("field,value", [("epoch", [1]), ("expert_valid", "yes"),
                                          ("submitted_action", ["bad"] * 14)])
def test_invalid_spool_row_does_not_poison_later_hdf5_batches(tmp_path, field, value):
    source = episode(tmp_path / "source", 0)
    directory = spool(source, (0, 2))
    row, images = sample(1)
    row[field] = value
    (directory / "000000000001.pkl").write_bytes(pickle.dumps((row, images)))
    output = tmp_path / "output"
    result = recover(source, output)
    assert result["frames"] == 2 and result["spool"]["unrecoverable_files"] == 1
    assert_frames(output, [0, 2])


def test_missing_manifest_is_explicit_and_does_not_create_output(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    spool(source, (0,))
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="manifest.*identity"):
        recover(source, output)
    assert not output.exists()


def test_killed_encoder_recovers_uncommitted_spool(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    encoder = EncoderProcess(source, 30, {"mock": True}, 60, 0, video_backend="libx264")
    try:
        for index in range(8):
            encoder.submit(*sample(index))
        deadline = time.monotonic() + 5
        while encoder.written.value < 8 and time.monotonic() < deadline:
            time.sleep(.01)
        assert encoder.written.value == 8
    finally:
        encoder.process.terminate()
        encoder.process.join(5)
        with pytest.raises(RuntimeError):
            encoder.close("aborted", {})
    before = fingerprint(source)
    output = tmp_path / "output"
    result = recover(source, output)
    assert result["frames"] == 8
    assert_frames(output, list(range(8)))
    assert fingerprint(source) == before
