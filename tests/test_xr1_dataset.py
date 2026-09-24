"""XR-1 cleaning must not silently turn policy or wait rows into expert data."""

import json

import numpy as np

from yam_abc_reproduce.hil.export import iter_expert_segments
from yam_abc_reproduce.hil.xr1_actions import XR1YamCodec
from yam_abc_reproduce.hil.xr1_dataset import (
    export_xr1_sidecar,
    iter_cleaned_expert_segments,
    iter_sidecar_segments,
)


def _row(tick, source="human", *, wait=False):
    state = [0.2, 1.1, 1.0, -0.3, 0.1, 0.2, 0.7,
             -0.1, 1.4, 0.8, 0.2, -0.1, 0.3, 0.3]
    action = state.copy()
    action[0] += tick * 0.002
    action[6] -= tick * 0.01
    return {
        "tick": tick, "time": tick / 30, "epoch": 1, "source": source,
        "expert_valid": source == "human", "observation_valid": True,
        "wait_boundary": wait, "observation_state": state,
        "submitted_action": action,
        "video_indices": {"top": tick, "left": tick, "right": tick},
    }


def test_xr1_cleaning_segments_and_native_window(tmp_path):
    source = tmp_path / "episode"
    source.mkdir()
    (source / "manifest.json").write_text(json.dumps({
        "schema": "yam_hil_v1", "fps": 30, "outcome": "success",
    }))
    rows = [_row(0), _row(1), _row(2), _row(3, wait=True),
            _row(4), _row(5, "policy"), _row(6)]
    (source / "steps.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    segments = list(iter_cleaned_expert_segments(source))
    assert [list(s.source_tick) for s in segments] == [[0, 1, 2], [4], [6]]
    first = segments[0]
    window = first.window(1)
    assert window["state"].shape == (1, 60)
    assert window["action"].shape == (30, 60)
    assert window["action_mask"].sum() == 28
    assert window["step_mask"].sum() == 2
    np.testing.assert_array_equal(window["source_tick"], [1, 2])
    np.testing.assert_array_equal(window["source_video_index"], [1, 1, 1])
    expected = XR1YamCodec().encode(first.observation_state[1], first.expert_action[1:3])
    np.testing.assert_allclose(window["action"][:2], expected, atol=1e-6)
    np.testing.assert_allclose(
        window["action"][2:], np.repeat(expected[-1:], 28, axis=0), atol=1e-6
    )

    output = tmp_path / "cleaned"
    assert export_xr1_sidecar(source, output) == 3
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["expert_only"] is True
    assert len(manifest["segments"]) == 3
    with np.load(output / "expert_000000.npz") as data:
        np.testing.assert_array_equal(data["source_tick"], [0, 1, 2])
        assert data["observation_pose"].shape == (3, 2, 4, 4)
    np.testing.assert_allclose(next(iter_sidecar_segments(output)).window(1)["action"],
                               window["action"], atol=1e-6)
    assert (source / "steps.jsonl").exists()  # source is read-only


def test_xr1_cleaning_rejects_non_30hz_and_existing_output(tmp_path):
    source = tmp_path / "episode"
    source.mkdir()
    (source / "steps.jsonl").write_text(json.dumps(_row(0)) + "\n")
    (source / "manifest.json").write_text(json.dumps({
        "schema": "yam_hil_v1", "fps": 20, "outcome": "success",
    }))
    import pytest
    with pytest.raises(ValueError, match="30 Hz"):
        list(iter_cleaned_expert_segments(source))
    (source / "manifest.json").write_text(json.dumps({
        "schema": "yam_hil_v1", "fps": 30, "outcome": "success",
    }))
    output = tmp_path / "cleaned"
    assert export_xr1_sidecar(source, output) == 1
    with pytest.raises(FileExistsError):
        export_xr1_sidecar(source, output)


def test_expert_selector_does_not_bridge_invalid_observation_or_storage_file():
    rows = [_row(0), _row(1), _row(2), _row(3), _row(4)]
    rows[0]["_segment"] = rows[1]["_segment"] = "a"
    rows[2]["_segment"] = "b"
    rows[2]["observation_valid"] = False
    rows[3]["_segment"] = rows[4]["_segment"] = "b"
    assert [[r["tick"] for r in part] for part in iter_expert_segments(rows)] == [
        [0, 1], [3, 4]
    ]
