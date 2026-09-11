# Synthetic, offline engineering cases; no model, training or hardware execution.
# ruff: noqa: PT009, PT027
import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "memory_gate.py"
SPEC = importlib.util.spec_from_file_location("engineering_gate", SCRIPT)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


class EngineeringMemoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scope = {"project": "demo", "platform": "edge"}
        self.write("docs/current.md", "# Runtime\n## Frequency check\nCheck measured pacing and units.\n")
        self.write("config.json", '{"expected_hz":30}')
        self.write("report.json", '{"observed_hz":500,"raw":"RAW_EVIDENCE_ONLY"}')
        self.write("scripts/probe.py", "from pathlib import Path\nPath('EXECUTED').touch()\n")
        gate.initialize(self.root, "case", [])

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def record(self, name="case", claim="latency frequency investigation"):
        return {
            "id": name,
            "claim": claim,
            "kind": "lesson",
            "owner": "docs/current.md",
            "scope": dict(self.scope),
            "status": "verified",
            "observed_at": "2026-01-01T00:00:00Z",
            "recorded_at": "2026-01-02T00:00:00Z",
            "valid_until": None,
            "recheck": "on_change",
            "depends_on": {"config.json": gate.digest(self.root / "config.json")},
            "evidence": [{"path": "report.json", "sha256": gate.digest(self.root / "report.json")}],
            "supersedes": [],
        }

    def save_record(self, record):
        source = f"docs/cache/records/{record['id']}.json"
        self.write(source, json.dumps(record, ensure_ascii=False))
        return source

    def capability(self, name="capability"):
        record = self.record(name)
        record["kind"] = "procedure"
        record["depends_on"]["scripts/probe.py"] = gate.digest(self.root / "scripts/probe.py")
        record["capability"] = {
            "entrypoint": "scripts/probe.py",
            "invocation": "python scripts/probe.py --config config.json",
            "config_paths": ["config.json"],
            "inputs": ["timestamped command stream in seconds"],
            "outputs": ["measured frequency in Hz"],
            "validation_command": "python scripts/probe.py --fixture fixed-capture.json",
            "acceptance": "frequency agrees with the fixed synthetic capture",
            "limitations": ["synthetic test only; no hardware readiness established"],
        }
        return record

    def attempt(self, name="attempt"):
        record = self.record(name)
        record["attempt"] = {
            "symptom": "推理延迟与频率异常",
            "hypothesis": "batch size alone explains the latency",
            "intervention": "lowered batch size",
            "observation": "latency remained high and pacing was not held fixed",
            "verdict": "inconclusive",
            "confounders": ["uncontrolled pacing"],
            "retry_when": "pacing is measured and held fixed",
        }
        return record

    def assumption(self):
        return {
            "name": "command pacing",
            "expected": "30",
            "observed": "500",
            "unit": "Hz",
            "observed_at": "2026-01-01T00:00:00Z",
            "check": "measure timestamps in the captured stream",
            "result": "mismatch",
            "recheck_when": "before reusing the runtime procedure on a new capture",
        }

    def state(self):
        return json.loads(gate.ledger_path(self.root, "case").read_text())

    def test_capability_is_traceable_but_never_executed(self):
        record = self.capability()
        source = self.save_record(record)
        gate.validate_record(self.root, record)
        item = gate.select(self.root, source, self.scope)
        self.assertIn("not_execution_authorization", item["admission"])
        self.assertIn("runtime_conditions_require_recheck", item["admission"])
        self.assertFalse((self.root / "EXECUTED").exists())
        self.write("scripts/probe.py", "# Changed behavior\n")
        with self.assertRaises(gate.GateError):
            gate.select(self.root, source, self.scope)

    def test_capability_requires_artifact_identity_and_complete_usage(self):
        for mutation in ("untracked_entrypoint", "missing_config", "no_acceptance", "no_limits", "wrong_kind"):
            record = self.capability()
            if mutation == "untracked_entrypoint":
                record["depends_on"].pop("scripts/probe.py")
            elif mutation == "missing_config":
                record["capability"]["config_paths"] = ["missing.json"]
            elif mutation == "no_acceptance":
                record["capability"].pop("acceptance")
            elif mutation == "no_limits":
                record["capability"]["limitations"] = []
            else:
                record["kind"] = "fact"
            with self.subTest(mutation=mutation), self.assertRaises(gate.GateError):
                gate.validate_record(self.root, record)

    def test_verified_attempt_is_not_a_repair_recommendation(self):
        record = self.attempt()
        source = self.save_record(record)
        item = gate.select(self.root, source, self.scope)
        self.assertIn("historical_attempt", item["admission"])
        self.assertIn("verdict_is_not_a_repair_recommendation", item["admission"])
        packet = json.loads(gate.search(self.root, "case", "推理延迟", self.scope))
        self.assertEqual(packet["items"][0]["attempt_verdict"], "inconclusive")
        self.assertEqual(packet["items"][0]["status"], "verified")
        for key, value in (("retry_when", ""), ("confounders", None), ("verdict", "always_wrong")):
            invalid = copy.deepcopy(record)
            invalid["attempt"][key] = value
            with self.subTest(key=key), self.assertRaises(gate.GateError):
                gate.validate_record(self.root, invalid)

    def test_runtime_mismatch_and_unknown_are_preserved_not_certified(self):
        record = self.capability()
        record["assumptions"] = [self.assumption()]
        source = self.save_record(record)
        item = gate.select(self.root, source, self.scope)
        observed = json.loads(item["text"])["assumptions"][0]
        self.assertEqual((observed["expected"], observed["observed"], observed["unit"]), ("30", "500", "Hz"))
        self.assertEqual(observed["result"], "mismatch")
        self.assertIn("runtime_conditions_require_recheck", item["admission"])
        record["assumptions"][0].update(result="unknown", observed=None, observed_at=None)
        gate.validate_record(self.root, record)
        for changes in (
            {"result": "match"},
            {"observed": "500", "observed_at": "9999-01-01T00:00:00Z"},
            {"observed": "500", "observed_at": "2026-01-03T00:00:00Z"},
        ):
            invalid = copy.deepcopy(record)
            invalid["assumptions"][0].update(changes)
            with self.assertRaises(gate.GateError):
                gate.validate_record(self.root, invalid)

    def test_routes_check_reference_ownership_without_expanding_them(self):
        record = self.record()
        record["retrieval"] = {
            "terms": ["频率", "pacing"],
            "related": [{"relation": "check", "source": "docs/current.md#Frequency check"}],
        }
        gate.validate_record(self.root, record)
        for source in ("../outside.md", "missing.md"):
            invalid = copy.deepcopy(record)
            invalid["retrieval"]["related"][0]["source"] = source
            with self.assertRaises(gate.GateError):
                gate.validate_record(self.root, invalid)

    def test_search_filters_scope_status_and_evidence_before_returning_hits(self):
        good = self.record("good")
        self.save_record(good)
        for name, changes in (
            ("server", {"scope": self.scope | {"platform": "server"}}),
            ("candidate", {"status": "candidate"}),
            ("expired", {"valid_until": "2025-01-01T00:00:00Z"}),
            ("broken", {"evidence": [{"path": "report.json", "sha256": "0" * 64}]}),
            ("unrelated", {"claim": "dataset download"}),
        ):
            self.save_record(self.record(name) | changes)
        result = json.loads(gate.search(self.root, "case", "latency", self.scope))
        self.assertEqual([item["source"] for item in result["items"]], ["docs/cache/records/good.json"])
        self.assertEqual(result["excluded"]["scope_mismatch"], 1)
        self.assertEqual(result["excluded"]["not_current"], 1)
        self.assertEqual(result["excluded"]["invalid"], 2)
        self.assertEqual(result["excluded"]["not_matched"], 1)

    def test_review_discovers_broken_candidates_without_promoting_them(self):
        record = self.attempt() | {"status": "candidate", "evidence": []}
        source = self.save_record(record)
        reviewed = json.loads(gate.search(self.root, "case", "latency", self.scope, purpose="review"))
        self.assertIn("review_only", reviewed["items"][0]["admission"])
        current = json.loads(gate.search(self.root, "case", "latency", self.scope))
        self.assertEqual(current["items"], [])
        with self.assertRaises(gate.GateError):
            gate.pack(self.root, "case", [source], [], self.scope)

    def test_search_is_bounded_counts_bytes_and_does_not_mark_full_record_read(self):
        record = self.capability()
        source = self.save_record(record)
        before = self.state()
        output = gate.search(self.root, "case", "latency", self.scope, limit=1500)
        self.assertLessEqual(len(output), 1500)
        self.assertEqual(self.state()["used"], before["used"] + len(output))
        self.assertEqual(self.state()["seen"], before["seen"])
        self.assertNotIn(b"RAW_EVIDENCE_ONLY", output)
        self.assertNotIn(b"validation_command", output)
        self.assertNotIn(b"invocation", output)
        full = json.loads(gate.pack(self.root, "case", [source], [], self.scope))
        self.assertEqual(full["items"][0]["source"], source)
        self.assertIn("validation_command", full["items"][0]["text"])

    def test_search_explains_whole_item_omission_and_obeys_explicit_quota(self):
        self.save_record(self.record("large", "latency " + "long complete claim " * 1000))
        output = json.loads(gate.search(self.root, "case", "latency", self.scope, limit=500))
        self.assertEqual(output["items"], [])
        self.assertEqual(output["excluded"]["byte_limit"], 1)
        gate.resize(self.root, "case", self.state()["used"] + 20, "Explicit test quota")
        before = self.state()
        with self.assertRaises(gate.GateError):
            gate.search(self.root, "case", "latency", self.scope)
        self.assertEqual(self.state(), before)

    def test_problem_keywords_rank_matches_and_no_project_is_inferred(self):
        preferred = self.record("two")
        preferred["retrieval"] = {"terms": ["推理延迟", "频率检查"]}
        self.save_record(preferred)
        self.save_record(self.record("one", "推理延迟"))
        result = json.loads(gate.search(self.root, "case", "推理延迟 频率检查", self.scope, top=1))
        self.assertEqual(result["items"][0]["source"], "docs/cache/records/two.json")
        self.assertEqual(result["excluded"]["top_limit"], 1)
        with self.assertRaises(gate.GateError):
            gate.search(self.root, "case", "推理延迟", {})

    def test_search_cannot_follow_an_escape_symlink(self):
        self.save_record(self.record())
        (self.root / "docs/cache/records/escape.json").symlink_to("/etc/passwd")
        result = json.loads(gate.search(self.root, "case", "latency", self.scope))
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["excluded"]["invalid"], 1)

    def test_cli_search_replays_a_synthetic_latency_case(self):
        check = self.capability("frequency-check")
        check["retrieval"] = {"terms": ["推理延迟", "频率"]}
        check["assumptions"] = [self.assumption()]
        self.save_record(check)
        self.save_record(self.attempt("batch-attempt"))
        self.save_record(self.record("server-only") | {"scope": self.scope | {"platform": "server"}})
        self.save_record(self.record("untested") | {"status": "candidate"})
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "search",
                "--root",
                str(self.root),
                "--session",
                "case",
                "--query",
                "推理延迟 频率",
                "--scope",
                "project=demo",
                "--scope",
                "platform=edge",
                "--top",
                "2",
                "--max-bytes",
                "2200",
            ],
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertLessEqual(len(result.stdout), 2200)
        hits = json.loads(result.stdout)["items"]
        self.assertEqual(
            {item["source"] for item in hits},
            {"docs/cache/records/frequency-check.json", "docs/cache/records/batch-attempt.json"},
        )
        full = json.loads(gate.pack(self.root, "case", [item["source"] for item in hits], [], self.scope))
        records = {json.loads(item["text"])["id"]: json.loads(item["text"]) for item in full["items"]}
        self.assertEqual(records["batch-attempt"]["attempt"]["verdict"], "inconclusive")
        self.assertEqual(records["frequency-check"]["assumptions"][0]["result"], "mismatch")
        self.assertFalse((self.root / "EXECUTED").exists())


if __name__ == "__main__":
    unittest.main()
