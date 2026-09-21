from yam_abc_reproduce.hil.intervention_recording import InterventionRecordingGate


def test_wait_has_no_samples_and_preserves_original_tick_and_boundary():
    gate = InterventionRecordingGate()
    def row(t):
        return dict(tick=t, time=t/30, intervention_id=1, source="hold")
    assert gate.filter(row(1), False)["tick"] == 1
    assert gate.filter(row(2), True) is None
    assert gate.filter(row(3), True) is None
    kept = gate.filter(dict(row(4), source="human"), False)
    assert kept["tick"] == 4 and kept["source"] == "human"
    assert kept["omitted_intervention_wait"]["frames"] == 2
    assert gate.filter(row(5), False).get("omitted_intervention_wait") is None
    assert gate.audit()[0]["last_tick"] == 3


def test_wait_reasons_are_distinct_and_new_episode_has_empty_audit():
    gate = InterventionRecordingGate()
    def row(t):
        return dict(tick=t, time=t/30, intervention_id=1)
    gate.filter(row(1), True, "takeover_wait")
    gate.filter(row(2), False)
    gate.filter(row(3), True, "handback_wait")
    out = gate.filter(row(4), False)
    assert out["omitted_intervention_wait"]["reason"] == "handback_wait"
    assert [x["reason"] for x in gate.audit()] == ["takeover_wait", "handback_wait"]
    assert InterventionRecordingGate().audit() == []


def test_compacted_clock_keeps_source_time_and_actions():
    gate = InterventionRecordingGate()
    phases = ["policy", "takeover", "human", "hold", "resume", "fault"]
    kept = []
    for tick, phase in enumerate(phases):
        row = dict(mode="hil", phase=phase, intervention_pending=True, intervention_id=1,
                   tick=tick, time=100 + tick / 30, submitted_action=[tick] * 14)
        result = gate.process(row, 30)
        if result is not None:
            kept.append(result)
            assert result["time"] == row["time"]
            assert result["submitted_action"] == row["submitted_action"]
    assert [r["tick"] for r in kept] == [0, 2, 4, 5]
    assert [r["frame_index"] for r in kept] == list(range(4))
    assert [r["timestamp"] for r in kept] == [i / 30 for i in range(4)]
    assert [r["wait_boundary"] for r in kept] == [False, True, True, False]


def test_rule_reload_is_per_episode(tmp_path, monkeypatch):
    import yam_abc_reproduce.hil.intervention_recording as rules
    from pathlib import Path
    path = tmp_path / "rules.py"
    source = Path(rules.__file__).read_text()
    path.write_text(source)
    monkeypatch.setattr(rules, "__file__", str(path))
    first = rules.load_recording_gate()
    path.write_text(source + "\n# next episode rule revision\n")
    second = rules.load_recording_gate()
    assert first.revision != second.revision
    row = dict(mode="hil", phase="policy", tick=1, time=1.)
    assert first.process(row, 30)["frame_index"] == 0
    assert first.process(row, 30)["frame_index"] == 1
    assert second.process(row, 30)["frame_index"] == 0
