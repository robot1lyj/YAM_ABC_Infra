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

    def test_default_retrieval_has_no_cumulative_stop_and_deduplicates(self):
        gate.pack(self.root, "case", ["docs/current.md"], [])
        before = self.state()
        with self.assertRaises(gate.GateError):
            gate.pack(self.root, "case", ["docs/current.md"], [])
        self.assertEqual(self.state(), before)
        with self.assertRaises(gate.GateError):
            gate.initialize(self.root, "case", ["AGENTS.md"])
        for index in range(8):
            self.write(f"docs/{index}.md", "x" * 6000)
            gate.pack(self.root, "case", [f"docs/{index}.md"], [])
        self.assertIsNone(self.state()["cap"])
        self.assertGreater(self.state()["used"], 48_000)
        report = gate.usage(self.state())
        self.assertEqual(report["tracked_bytes"], self.state()["used"])
        self.assertIsNone(report["context_tokens"])
        self.assertIsNone(report["remaining_transfer_bytes"])
        self.assertFalse(report["exact_token_enforcement"])

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

    def test_resize_preserves_accounting_and_rejects_invalid_limits(self):
        gate.pack(self.root, "case", ["docs/current.md"], [])
        before = self.state()
        gate.resize(self.root, "case", 196608, "user-authorized task expansion")
        after = self.state()
        for key in before:
            if key != "cap":
                self.assertEqual(after[key], before[key])
        self.assertEqual(after["cap"], 196608)
        self.assertEqual(after["adjustments"][0]["old_cap"], before["cap"])
        for cap, reason in ((0, "zero"), (-1, "negative"), (True, "boolean"), (1, "below used"), (65536, " ")):
            with self.assertRaises(gate.GateError):
                gate.resize(self.root, "case", cap, reason)
            self.assertEqual(self.state(), after)

    def test_path_escape_rejected(self):
        with self.assertRaises(gate.GateError):
            gate.select(self.root, "../outside.md", {})
        (self.root / "linked.md").symlink_to("/etc/passwd")
        with self.assertRaises(gate.GateError):
            gate.select(self.root, "linked.md", {})

    def test_required_section_can_exceed_default_packet_without_truncation(self):
        original = "## Gate\n" + "Evidence and conditions. " * 800 + "\nRequired final condition.\n"
        self.write("docs/large.md", original)
        before = self.state()
        with self.assertRaises(gate.GateError):
            gate.pack(self.root, "case", ["docs/large.md#Gate"], [])
        self.assertEqual(self.state(), before)
        output = gate.pack(self.root, "case", ["docs/large.md#Gate"], [], limit=25_000)
        self.assertGreater(len(output), gate.PACKET_BYTES)
        self.assertLessEqual(len(output), 25_000)
        self.assertEqual(json.loads(output)["items"][0]["text"], original)
        self.assertEqual((self.root / "docs/large.md").read_text(), original)

    def test_legacy_quota_is_preserved_until_explicit_removal(self):
        legacy = self.state() | {"cap": 32768}
        gate.save(gate.ledger_path(self.root, "case"), legacy)
        for index in range(5):
            self.write(f"docs/{index}.md", "x" * 6000)
            gate.pack(self.root, "case", [f"docs/{index}.md"], [])
        self.write("docs/next.md", "x" * 6000)
        before = self.state()
        with self.assertRaises(gate.GateError):
            gate.pack(self.root, "case", ["docs/next.md"], [])
        self.assertEqual(self.state(), before)
        gate.resize(self.root, "case", None, "Explicit project policy removed the transfer quota")
        after = self.state()
        self.assertEqual(after["used"], before["used"])
        self.assertEqual(after["seen"], before["seen"])
        self.assertIsNone(after["cap"])
        self.assertEqual(after["adjustments"][0]["old_cap"], 32768)
        self.assertIsNone(after["adjustments"][0]["new_cap"])
        gate.pack(self.root, "case", ["docs/next.md"], [])
        self.assertGreater(self.state()["used"], 32768)

    def test_reload_restores_selected_excerpt_and_counts_it_once(self):
        spec = "docs/current.md#Gate"
        gate.pack(self.root, "case", [spec], [])
        before = self.state()["used"]
        output = gate.pack(self.root, "case", [spec, spec], [], reload=True)
        packet = json.loads(output)
        self.assertEqual(len(packet["items"]), 1)
        self.assertEqual(packet["unchanged"], 1)
        self.assertEqual(self.state()["used"], before + len(output))
        self.assertNotIn("Cold details", packet["items"][0]["text"])

    def test_reload_cannot_bypass_evidence_or_scope(self):
        record = self.record()
        spec = "docs/cache/records/reload.json"
        self.write(spec, json.dumps(record))
        gate.pack(self.root, "case", [spec], [], record["scope"])
        before = self.state()
        with self.assertRaises(gate.GateError):
            gate.pack(self.root, "case", [spec], [], {}, reload=True)
        self.write("norm.json", '{"scale":2}')
        with self.assertRaises(gate.GateError):
            gate.pack(self.root, "case", [spec], [], record["scope"], reload=True)
        self.assertEqual(self.state(), before)

    def test_preloads_are_optional_deduplicated_and_not_context_measurement(self):
        report = gate.initialize(self.root, "empty", [])
        self.assertEqual(report["tracked_bytes"], 0)
        self.assertIsNone(report["context_tokens"])
        size = len((self.root / "AGENTS.md").read_bytes())
        report = gate.initialize(self.root, "preloaded", ["AGENTS.md", "AGENTS.md"], cap=size)
        self.assertEqual(report["tracked_bytes"], size)
        self.assertEqual(report["remaining_transfer_bytes"], 0)

    def test_explicit_quota_accepts_large_configuration_and_remains_atomic(self):
        report = gate.initialize(self.root, "large", [], cap=1_000_000)
        self.assertEqual(report["transfer_limit_bytes"], 1_000_000)
        gate.initialize(self.root, "small", [], cap=50)
        before = gate.ledger_path(self.root, "small").read_bytes()
        with self.assertRaises(gate.GateError):
            gate.pack(self.root, "small", ["docs/current.md"], [], limit=25_000)
        self.assertEqual(gate.ledger_path(self.root, "small").read_bytes(), before)

    def test_concurrent_packers_cannot_overspend(self):
        gate.resize(self.root, "case", 32768, "Explicit quota for concurrent retrieval")
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
        self.assertLessEqual(self.state()["used"], 32768)

    def test_cli_init_resize_reload_and_audit(self):
        def run(*args):
            result = subprocess.run(
                [sys.executable, str(SCRIPT), *args, "--root", str(self.root), "--session", "cli"],
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            return json.loads(result.stdout)

        self.assertIsNone(run("init")["transfer_limit_bytes"])
        run("pack", "--required", "docs/current.md")
        run("pack", "--required", "docs/current.md", "--reload", "--max-bytes", "20000")
        before = run("audit")
        run("resize", "--context-bytes", "65536", "--reason", "Explicit quota")
        after = run("resize", "--no-total-limit", "--reason", "Quota removed by project policy")
        self.assertEqual(after["tracked_bytes"], before["tracked_bytes"])
        self.assertIsNone(after["transfer_limit_bytes"])
        self.assertIsNone(after["context_tokens"])

    def test_request_cli_requires_explicit_byte_limit_and_does_not_claim_token_enforcement(self):
        request = self.write("request.json", json.dumps({"messages": [{"content": "x" * 100000}]}))
        command = [sys.executable, str(SCRIPT), "request", "--input", str(request)]
        missing_limit = subprocess.run(command, capture_output=True, check=False)
        self.assertEqual(missing_limit.returncode, 2)
        for limit, code in ((100, 2), (110000, 0)):
            result = subprocess.run([*command, "--max-bytes", str(limit)], capture_output=True, check=False)
            self.assertEqual(result.returncode, code)
            if code == 0:
                report = json.loads(result.stdout)
                self.assertEqual(report["request_bytes"], request.stat().st_size)
                self.assertFalse(report["exact_token_enforcement"])
            else:
                self.assertEqual(result.stdout, b"")

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
