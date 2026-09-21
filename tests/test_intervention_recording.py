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
