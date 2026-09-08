import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_project_memory import check


def test_document_route_detects_missing_link(tmp_path):
    (tmp_path / "docs/cache/records").mkdir(parents=True)
    (tmp_path / "docs/cache/context_index.md").write_text("[owner](../owner.md)")
    (tmp_path / "docs/owner.md").write_text("[broken](missing.md)")
    for name in ("README.md", "AGENTS.md"):
        (tmp_path / name).write_text("")
    assert any("失效文件链接" in e for e in check(tmp_path)["errors"])
    (tmp_path / "docs/missing.md").write_text("")
    assert not check(tmp_path)["errors"]


def test_verified_hash_change_fails_but_historical_record_is_review_only(tmp_path):
    import datetime
    import hashlib

    (tmp_path / "docs/cache/records").mkdir(parents=True)
    (tmp_path / "docs/cache/context_index.md").write_text("[owner](../owner.md)")
    (tmp_path / "docs/owner.md").write_text("")
    for name in ("README.md", "AGENTS.md"):
        (tmp_path / name).write_text("")
    (tmp_path / "evidence.txt").write_text("original")
    now = datetime.datetime.now(datetime.UTC).isoformat()
    record = dict(
        id="test",
        kind="observation",
        claim="test only",
        owner="docs/owner.md",
        scope={"project": "test"},
        status="verified",
        observed_at=now,
        recorded_at=now,
        evidence=[{"path": "evidence.txt", "sha256": hashlib.sha256(b"original").hexdigest()}],
        recheck="always",
        valid_until=None,
        depends_on={},
        supersedes=[],
    )
    path = tmp_path / "docs/cache/records/test.json"
    path.write_text(json.dumps(record))
    assert not check(tmp_path)["errors"]
    (tmp_path / "evidence.txt").write_text("tampered")
    assert check(tmp_path)["errors"]
    record["status"] = "stale"
    path.write_text(json.dumps(record))
    result = check(tmp_path)
    assert not result["errors"] and result["review_only"] and result["validated_records"] == 0
