# This suite intentionally runs without pytest on disconnected hosts.
# ruff: noqa: PT009, PT027
import concurrent.futures
import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "memory_gate.py"
SPEC = importlib.util.spec_from_file_location("memory_gate", SCRIPT)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


class MemoryGateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.write("AGENTS.md", "Mandatory project boundaries\n")
        self.write("docs/current.md", "# Current\n\n## Gate\nKeep all safety conditions.\n\n## Other\nCold details.\n")
        gate.initialize(self.root, "case", ["AGENTS.md"])

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def state(self):
        return json.loads(gate.ledger_path(self.root, "case").read_text())

    def record(self):
        evidence = self.write("report.json", '{"numerical_gate":"pass"}')
        norm = self.write("norm.json", '{"scale":1}')
        return {
            "id": "case-1",
            "kind": "lesson",
            "claim": "This exact configuration passed the numerical gate.",
            "owner": "docs/current.md",
            "scope": {"project": "condapi", "platform": "thor", "contract": "yam14-h50"},
            "status": "verified",
            "observed_at": "2026-01-01T00:00:00Z",
            "recorded_at": "2026-01-01T01:00:00Z",
            "valid_until": None,
            "recheck": "on_change",
            "depends_on": {"norm.json": gate.digest(norm)},
            "evidence": [{"path": "report.json", "sha256": gate.digest(evidence)}],
            "supersedes": [],
        }

    def test_unicode_and_json_framing_are_counted(self):
        original = '中文🙂"\\\n' * 800
        self.write("docs/large.md", original)
        before = self.state()["used"]
        output = gate.pack(self.root, "case", ["docs/current.md#Gate"], ["docs/large.md"], limit=500)
        self.assertLessEqual(len(output), 500)
        self.assertEqual(self.state()["used"] - before, len(output))
        self.assertEqual(json.loads(output)["omitted"], 1)
        self.assertEqual((self.root / "docs/large.md").read_text(), original)

    def test_required_overflow_is_atomic(self):
        self.write("docs/large.md", "safety " * 5000)
        before = self.state()
        with self.assertRaises(gate.GateError):
            gate.pack(self.root, "case", ["docs/current.md", "docs/large.md"], [])
        self.assertEqual(self.state(), before)

    def test_cumulative_and_duplicate_admission(self):
        gate.pack(self.root, "case", ["docs/current.md"], [])
        before = self.state()
        with self.assertRaises(gate.GateError):
            gate.pack(self.root, "case", ["docs/current.md"], [])
        self.assertEqual(self.state(), before)
        with self.assertRaises(gate.GateError):
            gate.initialize(self.root, "case", ["AGENTS.md"])
        for index in range(8):
            self.write(f"docs/{index}.md", "x" * 6000)
            try:
                gate.pack(self.root, "case", [f"docs/{index}.md"], [])
            except gate.GateError:
                break
        self.assertLessEqual(self.state()["used"], gate.CONTEXT_BYTES)
        self.assertGreater(self.state()["used"], 24_000)

    def test_sections_keep_children_and_ignore_code_headings(self):
        self.write("docs/sections.md", "## Gate\n```text\n## Fake\n```\n### Child\nkeep\n## End\nomit\n")
        item = gate.select(self.root, "docs/sections.md#Gate", {})
        self.assertIn("### Child", item["text"])
        self.assertNotIn("omit", item["text"])
        self.write("docs/sections.md", "## Gate\na\n## Gate\nb\n")
        with self.assertRaises(gate.GateError):
            gate.select(self.root, "docs/sections.md#Gate", {})

    def test_changed_source_is_charged_again(self):
        gate.pack(self.root, "case", ["docs/current.md"], [])
        before = self.state()["used"]
        self.write("docs/current.md", "Updated measured result")
        output = gate.pack(self.root, "case", ["docs/current.md"], [])
        self.assertEqual(self.state()["used"], before + len(output))

    def test_path_escape_and_budget_increase_rejected(self):
        with self.assertRaises(gate.GateError):
            gate.select(self.root, "../outside.md", {})
        (self.root / "linked.md").symlink_to("/etc/passwd")
        with self.assertRaises(gate.GateError):
            gate.select(self.root, "linked.md", {})
        with self.assertRaises(gate.GateError):
            gate.pack(self.root, "case", ["docs/current.md"], [], limit=gate.PACKET_BYTES + 1)

    def test_concurrent_packers_cannot_overspend(self):
        for index in range(8):
            self.write(f"docs/concurrent{index}.md", "x" * 6000)

        def execute(index):
            try:
                return len(gate.pack(self.root, "case", [f"docs/concurrent{index}.md"], []))
            except gate.GateError:
                return 0

        before = self.state()["used"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            sizes = list(pool.map(execute, range(8)))
        self.assertEqual(self.state()["used"], before + sum(sizes))
        self.assertLessEqual(self.state()["used"], gate.CONTEXT_BYTES)

    def test_scope_and_fingerprints_gate_reuse(self):
        record = self.record()
        spec = "docs/cache/records/case.json"
        self.write(spec, json.dumps(record))
        gate.select(self.root, spec, record["scope"])
        for scope in ({}, {"project": "condapi", "platform": "server", "contract": "yam14-h50"}):
            with self.assertRaises(gate.GateError):
                gate.select(self.root, spec, scope)
        self.write("norm.json", '{"scale":2}')
        with self.assertRaises(gate.GateError):
            gate.select(self.root, spec, record["scope"])

    def test_expiry_missing_evidence_and_live_observations(self):
        record = self.record()
        gate.validate_record(self.root, record)
        for changes in (
            {"evidence": []},
            {"valid_until": "2025-01-01T00:00:00Z"},
            {"observed_at": "2999-01-01T00:00:00Z"},
            {"observed_at": "2026-01-01"},
            {"depends_on": {}},
        ):
            with self.subTest(changes=changes), self.assertRaises(gate.GateError):
                gate.validate_record(self.root, record | changes)
        self.write("docs/cache/records/live.json", json.dumps(record | {"recheck": "always"}))
        with self.assertRaises(gate.GateError):
            gate.select(self.root, "docs/cache/records/live.json", record["scope"])

    def test_candidates_and_corrupt_evidence_are_not_recalled(self):
        record = self.record()
        self.write("docs/cache/records/candidate.json", json.dumps(record | {"status": "candidate"}))
        with self.assertRaises(gate.GateError):
            gate.select(self.root, "docs/cache/records/candidate.json", record["scope"])
        bad = copy.deepcopy(record)
        bad["evidence"][0]["sha256"] = "0" * 64
        with self.assertRaises(gate.GateError):
            gate.validate_record(self.root, bad)

    def test_host_token_budget_includes_reserve(self):
        request = {"messages": [{"role": "user", "content": "hi"}], "tools": [{"name": "test"}]}

        def counter(actual):
            self.assertEqual(actual, request)
            return 100

        self.assertEqual(gate.guard_request(request, counter, max_context_tokens=120, output_reserve=20), 100)
        for count in (101, -1, 1.5, True):
            with self.subTest(count=count), self.assertRaises(gate.GateError):
                gate.guard_request(request, lambda _, n=count: n, max_context_tokens=120, output_reserve=20)
        with self.assertRaises(gate.GateError):
            gate.guard_request(request, None, max_context_tokens=120, output_reserve=20)

    def test_cli_failure_emits_no_source(self):
        self.write("docs/secret.md", "DO_NOT_EMIT_THIS " * 3000)
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "pack",
                "--root",
                str(self.root),
                "--session",
                "case",
                "--required",
                "docs/secret.md",
            ],
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b"")
        self.assertNotIn(b"DO_NOT_EMIT_THIS", result.stderr)

    def test_record_location_does_not_bypass_scope_or_candidate_gate(self):
        record = self.record()
        self.write("relocated.json", json.dumps(record))
        with self.assertRaises(gate.GateError):
            gate.select(self.root, "relocated.json", {})
        self.write("relocated.json", json.dumps(record | {"status": "candidate"}))
        with self.assertRaises(gate.GateError):
            gate.select(self.root, "relocated.json", record["scope"])

    def test_nested_fence_preserves_required_section(self):
        self.write("docs/fenced.md", "## Gate\n````text\n```\n## Fake\n````\nkeep\n## End\nomit\n")
        item = gate.select(self.root, "docs/fenced.md#Gate", {})
        self.assertIn("keep", item["text"])
        self.assertNotIn("omit", item["text"])

    def test_malformed_record_and_self_supersession_rejected(self):
        for record in ([], self.record() | {"supersedes": ["case-1"]}):
            with self.assertRaises(gate.GateError):
                gate.validate_record(self.root, record)

    def test_review_can_inspect_but_cannot_promote_candidate(self):
        record = self.record() | {"status": "candidate", "evidence": []}
        spec = "docs/cache/records/candidate.json"
        self.write(spec, json.dumps(record))
        output = gate.pack(self.root, "case", [spec], [], purpose="review")
        self.assertIn("review_only", json.loads(output)["items"][0]["admission"])
        with self.assertRaises(gate.GateError):
            gate.pack(self.root, "case", [spec], [], record["scope"])

    def test_review_does_not_bypass_later_scope_check(self):
        record = self.record()
        spec = "docs/cache/records/verified.json"
        self.write(spec, json.dumps(record))
        gate.pack(self.root, "case", [spec], [], purpose="review")
        with self.assertRaises(gate.GateError):
            gate.pack(self.root, "case", [spec], [])
        output = gate.pack(self.root, "case", [spec], [], record["scope"])
        self.assertIn("evidence_and_scope_checked", json.loads(output)["items"][0]["admission"])

    def test_review_keeps_expired_evidence_for_audit_only(self):
        record = self.record() | {"valid_until": "2025-01-01T00:00:00Z"}
        spec = "docs/cache/records/expired.json"
        self.write(spec, json.dumps(record))
        gate.select(self.root, spec, {}, purpose="review")
        with self.assertRaises(gate.GateError):
            gate.select(self.root, spec, record["scope"])


if __name__ == "__main__":
    unittest.main()
