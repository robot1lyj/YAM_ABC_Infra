import json

import numpy as np
import pytest

from yam_abc_reproduce.hil.replay_policy import ReplayPolicy, load_targets


def test_replay_preserves_targets_and_holds_last_without_looping():
    targets = np.zeros((62, 14))
    targets[:, 0] = np.arange(62) / 100
    targets[:, 6] = .4
    replay = ReplayPolicy(targets)
    obs = {"observation.state": targets[0]}
    first, second, last = [replay.infer(obs) for _ in range(3)]
    np.testing.assert_array_equal(first["actions"], targets[:50])
    np.testing.assert_array_equal(second["actions"][:12], targets[50:])
    np.testing.assert_array_equal(second["actions"][12:], np.tile(targets[-1], (38, 1)))
    np.testing.assert_array_equal(last["actions"], np.tile(targets[-1], (50, 1)))
    assert last["server_timing"]["source"] == "recorded_replay"
    assert last["server_timing"]["rtc_used"] is False


def test_replay_refuses_rtc_and_unaligned_start_without_consuming_frames():
    replay = ReplayPolicy(np.zeros((50, 14)))
    with pytest.raises(ValueError, match="sync_hold"):
        replay.infer({"rtc": {}})
    with pytest.raises(ValueError, match="start pose"):
        replay.infer({"observation.state": np.ones(14)})
    assert replay.cursor == 0


def test_replay_loading_uses_submitted_not_leader_or_model(tmp_path, monkeypatch):
    from yam_abc_reproduce.hil import replay_policy

    (tmp_path / "manifest.json").write_text(json.dumps({"fps": 30, "outcome": "unknown"}))
    rows = [{"tick": n, "submitted_action": [.1] * 14,
             "leader_state": [.8] * 14, "policy_action": [.5] * 14} for n in range(3)]
    monkeypatch.setattr(replay_policy, "read_rows", lambda _: iter(rows))
    np.testing.assert_allclose(load_targets(tmp_path, start=1, steps=2), .1)
    assert load_targets(tmp_path, steps=None).shape == (3, 14)
    rows[-1]["tick"] = 4
    with pytest.raises(ValueError, match="tick gap"):
        load_targets(tmp_path)
