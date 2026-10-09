import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_project_memory import check, report


def add_hot_files(root):
    for name in ("kernel", "checkpoint"):
        (root / f"docs/cache/{name}.md").write_text("", encoding="utf-8")


def test_document_route_detects_missing_link(tmp_path):
    (tmp_path / "docs/cache/records").mkdir(parents=True)
    add_hot_files(tmp_path)
    (tmp_path / "docs/cache/context_index.md").write_text("[owner](../owner.md)")
    (tmp_path / "docs/owner.md").write_text("[broken](missing.md)")
    for name in ("README.md", "AGENTS.md"):
        (tmp_path / name).write_text("")
    assert any("失效文件链接" in e for e in check(tmp_path)["errors"])
    (tmp_path / "docs/missing.md").write_text("")
    assert not check(tmp_path)["errors"]


def test_hash_change_only_blocks_strict_and_history_remains_review_only(tmp_path):
    import datetime
    import hashlib

    (tmp_path / "docs/cache/records").mkdir(parents=True)
    add_hot_files(tmp_path)
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
    assert not check(tmp_path)["errors"]
    assert not check(tmp_path)["fingerprints_checked"]
    assert check(tmp_path, integrity="strict")["errors"]
    record["status"] = "stale"
    path.write_text(json.dumps(record))
    result = check(tmp_path, integrity="strict")
    assert not result["errors"] and result["review_only"] and result["validated_records"] == 0


def test_hot_budget_counts_utf8_and_requires_files(tmp_path):
    from check_project_memory import hot_budget

    (tmp_path / "docs/cache").mkdir(parents=True)
    add_hot_files(tmp_path)
    (tmp_path / "AGENTS.md").write_text("中" * 2049, encoding="utf-8")
    (tmp_path / "docs/cache/context_index.md").write_text("", encoding="utf-8")
    budget, errors = hot_budget(tmp_path)
    assert budget["files"]["AGENTS.md"] == 6147
    assert any("热记忆超限" in error for error in errors)
    (tmp_path / "docs/cache/kernel.md").write_text("- theme\n" * 9)
    assert "kernel主题超过8条" in hot_budget(tmp_path)[1]
    (tmp_path / "docs/cache/checkpoint.md").unlink()
    assert any("缺失热记忆" in error for error in hot_budget(tmp_path)[1])


def test_current_hot_links_validate_chinese_and_duplicate_anchors(tmp_path):
    (tmp_path / "docs/cache/records").mkdir(parents=True)
    add_hot_files(tmp_path)
    (tmp_path / "AGENTS.md").write_text("")
    (tmp_path / "README.md").write_text("")
    (tmp_path / "docs/owner.md").write_text("## 中文：`tick`（当前）\n## Repeat\n## Repeat\n")
    (tmp_path / "docs/cache/context_index.md").write_text("[owner](../owner.md#中文tick当前)")
    checkpoint = tmp_path / "docs/cache/checkpoint.md"
    checkpoint.write_text("[repeat](../owner.md#repeat-1)")
    assert not check(tmp_path)["errors"]
    checkpoint.write_text("[missing](../owner.md#missing)")
    assert any("失效章节链接" in error for error in check(tmp_path)["errors"])


def test_hot_budget_aggregate_at_exact_limits(tmp_path):
    from check_project_memory import HOT_LIMITS, hot_budget

    (tmp_path / "docs/cache").mkdir(parents=True)
    for name, limit in HOT_LIMITS.items():
        (tmp_path / name).write_text("x" * limit)
    budget, errors = hot_budget(tmp_path)
    assert budget["rules_bytes"] == 3584
    assert budget["routed_bytes"] == 6656
    assert budget["overview_bytes"] == 8192
    assert budget["resumed_bytes"] == 9216
    assert not errors


def test_missing_route_reports_error_without_crashing(tmp_path):
    result = check(tmp_path)
    assert any("context_index.md" in error for error in result["errors"])


def test_default_report_summarizes_history_without_hiding_errors():
    result = {"errors": ["bad link"], "review_only": ["old-a", "old-b"], "validated_records": 1}
    compact = report(result)
    assert compact["errors"] == ["bad link"]
    assert compact["review_only_count"] == 2
    assert "review_only" not in compact
    assert report(result, details=True) is result


def test_basic_missing_source_warns_and_does_not_count_as_ready(tmp_path):
    import datetime

    (tmp_path / "docs/cache/records").mkdir(parents=True)
    add_hot_files(tmp_path)
    (tmp_path / "docs/cache/context_index.md").write_text("[owner](../owner.md)")
    (tmp_path / "docs/owner.md").write_text("")
    for name in ("README.md", "AGENTS.md"):
        (tmp_path / name).write_text("")
    now = datetime.datetime.now(datetime.UTC).isoformat()
    record = dict(
        id="missing", kind="observation", claim="synthetic source not available",
        owner="docs/owner.md", scope={"project": "test"}, status="verified",
        observed_at=now, recorded_at=now, evidence=[{"path": "missing.txt"}],
        recheck="on_change", valid_until=None, supersedes=[],
    )
    (tmp_path / "docs/cache/records/missing.json").write_text(json.dumps(record))
    result = check(tmp_path)
    assert not result["errors"] and result["validated_records"] == 0
    assert "source_unavailable" in result["source_warnings"][0]
    assert result["review_only"] and check(tmp_path, integrity="strict")["errors"]


def test_actual_hot_routes_keep_dynamic_addresses_at_the_owner():
    import re

    root = Path(__file__).resolve().parents[1]
    for name in (
        "AGENTS.md", "docs/cache/kernel.md", "docs/cache/context_index.md",
        "docs/cache/problem_index.md", "docs/cache/checkpoint.md",
    ):
        assert not re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", (root / name).read_text()), name
    index = (root / "docs/cache/problem_index.md").read_text()
    assert "../environment.md#ipc连接与地址" in index
    checkpoint = (root / "docs/cache/checkpoint.md").read_text()
    assert "不代表现在状态" in checkpoint and "不等于硬件复验成功" in checkpoint


def test_project_basic_check_does_not_hash_artifacts(tmp_path, monkeypatch):
    import memory_gate

    test_basic_missing_source_warns_and_does_not_count_as_ready(tmp_path)
    (tmp_path / "missing.txt").write_text("synthetic source now available")

    def forbidden(_):
        raise AssertionError("basic checks must not hash evidence")

    monkeypatch.setattr(memory_gate, "digest", forbidden)
    result = check(tmp_path)
    assert not result["errors"] and not result["source_warnings"]
    assert result["validated_records"] == 1 and not result["fingerprints_checked"]
