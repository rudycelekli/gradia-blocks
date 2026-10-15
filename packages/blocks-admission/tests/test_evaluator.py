import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gradia_admission.evaluator import InputError, artifact_digest, digest, evaluate, main, protocol, read_json


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "artifact"
        self.root.mkdir()
        (self.root / "source.py").write_text("raise RuntimeError('must never execute')\n")
        self.manifest = {"schema_version": 1, "candidate_id": "example.feedback/0.1",
                         "profiles": ["service", "ui"],
                         "provenance": {"source": "https://example.invalid/source", "revision": "abc123", "license_expression": "Apache-2.0"},
                         "budgets": {"max_cost_usd": 2, "max_duration_seconds": 120},
                         "privacy": {"tenant_boundary": "isolated", "raw_content_default": "off", "telemetry_export": "off"}}
        self.spec = protocol()
        gates = self.spec["base_gates"] + ["accessibility"]
        self.policy = {"schema_version": 1, "protocol_sha256": digest(self.spec),
                       "limits": {"max_cost_usd": 2, "max_duration_seconds": 120},
                       "reviewers": {"fixture-reviewer": {"kind": "human", "gates": gates}}, "receipt_sha256": []}
        # Synthetic receipts exercise the evaluator. They are not admission evidence for any real block.
        self.receipts = [{"schema_version": 1, "candidate_id": self.manifest["candidate_id"],
                          "artifact_sha256": artifact_digest(self.root), "manifest_sha256": digest(self.manifest),
                          "protocol_sha256": digest(self.spec), "gate": gate,
                          "reviewer_id": "fixture-reviewer", "verdict": "PASS",
                          "checks": {"total": 1, "passed": 1, "failed": 0, "skipped": 0},
                          "evidence_refs": ["fixture://synthetic-check"],
                          "measurements": {"cost_usd": 0, "duration_seconds": 1}}
                         for gate in gates]
        self.pin()

    def pin(self):
        self.policy["receipt_sha256"] = sorted({digest(receipt) for receipt in self.receipts})

    def result(self):
        return evaluate(self.manifest, self.root, self.receipts, self.policy)

    def assert_blocked(self):
        result = self.result()
        self.assertEqual(result["decision"], "BLOCKED")
        self.assertFalse(result["promotion_authorized"])

    def test_complete_pinned_evidence_only_eligible_for_review(self):
        result = self.result()
        self.assertEqual(result["decision"], "ELIGIBLE_FOR_REVIEW")
        self.assertFalse(result["promotion_authorized"])

    def test_missing_gate_unknown(self):
        self.receipts.pop()
        self.assert_blocked()
        self.assertEqual(next(r for r in self.result()["gates"] if r["gate"] == "accessibility")["status"], "UNKNOWN")

    def test_stale_artifact(self):
        (self.root / "source.py").write_text("new implementation")
        self.assert_blocked()

    def test_profile_stripping_invalidates_receipts(self):
        self.manifest["profiles"] = ["service"]
        self.receipts = [r for r in self.receipts if r["gate"] != "accessibility"]
        self.assert_blocked()

    def test_self_declared_candidate_approval_not_trusted(self):
        self.manifest["approved"] = True
        self.manifest["reviewers"] = self.policy["reviewers"]
        self.policy["receipt_sha256"] = []
        self.assert_blocked()

    def test_mutated_receipt_requires_independent_repin(self):
        self.receipts[0]["evidence_refs"] = ["changed"]
        self.assert_blocked()

    def test_reviewer_scopes_are_exact_lists_not_substring_membership(self):
        self.policy["reviewers"]["fixture-reviewer"]["gates"] = "prefix_" + "_".join(self.spec["base_gates"] + ["accessibility"]) + "_suffix"
        with self.assertRaises(InputError):
            self.result()

    def test_malformed_reviewer_policy_fails_with_input_error(self):
        for reviewer in [None, [], {"kind": "human", "gates": ["security", "security"]},
                         {"kind": "human", "gates": ["not_security"]},
                         {"kind": "invented", "gates": ["security"]}]:
            with self.subTest(reviewer=reviewer):
                self.policy["reviewers"]["fixture-reviewer"] = reviewer
                with self.assertRaises(InputError):
                    self.result()

    def test_malformed_pin_and_receipt_scope_fail_closed(self):
        self.policy["receipt_sha256"] = ["not-a-digest"]
        with self.assertRaises(InputError):
            self.result()
        self.pin()
        self.receipts[0]["reviewer_id"] = []
        with self.assertRaises(InputError):
            self.result()

    def test_entry_budget_counts_empty_and_excluded_directories(self):
        for name in ["a", "b", "node_modules"]:
            (self.root / name).mkdir()
        with patch("gradia_admission.evaluator.MAX_ARTIFACT_ENTRIES", 3):
            with self.assertRaisesRegex(InputError, "entry budget"):
                artifact_digest(self.root)

    def test_directory_depth_budget(self):
        nested = self.root / "a" / "b" / "c"
        nested.mkdir(parents=True)
        with patch("gradia_admission.evaluator.MAX_ARTIFACT_DEPTH", 2):
            with self.assertRaisesRegex(InputError, "depth budget"):
                artifact_digest(self.root)

    def test_unknown_verdict_blocks(self):
        self.receipts[0]["verdict"] = "UNKNOWN"
        self.pin()
        self.assert_blocked()

    def test_explicit_failure_preserved(self):
        self.receipts[0]["verdict"] = "FAIL"
        self.pin()
        self.assert_blocked()
        self.assertIn("FAIL", [r["status"] for r in self.result()["gates"]])

    def test_check_count_mutants(self):
        for counts in [{"total": 0, "passed": 0, "failed": 0, "skipped": 0},
                       {"total": 1, "passed": 1, "failed": 0, "skipped": 1},
                       {"total": 1, "passed": 1, "failed": 1, "skipped": 0},
                       {"total": True, "passed": True, "failed": 0, "skipped": 0},
                       {"total": 2, "passed": 1, "failed": 0, "skipped": 0}]:
            with self.subTest(counts=counts):
                self.receipts[0]["checks"] = counts
                self.pin()
                self.assert_blocked()

    def test_unknown_or_duplicate_gate(self):
        original = copy.deepcopy(self.receipts)
        for gate in ["invented_gate", self.receipts[0]["gate"]]:
            self.receipts = copy.deepcopy(original)
            extra = copy.deepcopy(self.receipts[0])
            extra["gate"] = gate
            self.receipts.append(extra)
            self.pin()
            self.assert_blocked()

    def test_unsupported_profile_rejected(self):
        self.manifest["profiles"] = ["all_languages"]
        with self.assertRaises(InputError):
            self.result()

    def test_operator_budget_cannot_be_widened_by_candidate(self):
        self.manifest["budgets"]["max_cost_usd"] = 999
        self.assert_blocked()

    def test_missing_or_over_budget_measurements(self):
        for measurements in [{}, {"cost_usd": 3, "duration_seconds": 1},
                             {"cost_usd": 0, "duration_seconds": 121},
                             {"cost_usd": -1, "duration_seconds": 1}]:
            with self.subTest(measurements=measurements):
                self.receipts[0]["measurements"] = measurements
                self.pin()
                self.assert_blocked()

    def test_tenant_content_export_mutants(self):
        for key, value in [("tenant_boundary", "shared"), ("raw_content_default", "on"),
                           ("telemetry_export", "always")]:
            with self.subTest(key=key):
                old = self.manifest["privacy"][key]
                self.manifest["privacy"][key] = value
                self.assert_blocked()
                self.manifest["privacy"][key] = old

    def test_human_rights_cannot_be_replaced_by_ai_receipt(self):
        self.policy["reviewers"]["fixture-reviewer"]["kind"] = "automated"
        self.assert_blocked()

    def test_stale_protocol_policy(self):
        self.policy["protocol_sha256"] = "stale"
        self.assert_blocked()

    def test_tree_hash_includes_tests_ignores_caches(self):
        before = artifact_digest(self.root)
        cache = self.root / "__pycache__"
        cache.mkdir()
        (cache / "cache.pyc").write_text("not source")
        self.assertEqual(before, artifact_digest(self.root))
        (self.root / "test_source.py").write_text("assert True")
        self.assertNotEqual(before, artifact_digest(self.root))

    def test_symlink_and_empty_artifact_rejected(self):
        (self.root / "link").symlink_to(self.root / "source.py")
        with self.assertRaises(InputError):
            artifact_digest(self.root)
        (self.root / "link").unlink()
        (self.root / "source.py").unlink()
        with self.assertRaises(InputError):
            artifact_digest(self.root)

    def test_duplicate_keys_and_nonfinite_json_rejected(self):
        target = Path(self.temp.name) / "invalid.json"
        for text in ['{"verdict":"FAIL","verdict":"PASS"}', '{"cost":NaN}']:
            target.write_text(text)
            with self.assertRaises(InputError):
                read_json(target)

    def test_canonical_hash_stable_across_key_order(self):
        self.assertEqual(digest({"a": 1, "b": [2]}), digest({"b": [2], "a": 1}))

    def test_receipt_count_budget(self):
        with self.assertRaises(InputError):
            evaluate(self.manifest, self.root, self.receipts * 6, self.policy)

    def test_json_control_input_budget(self):
        target = Path(self.temp.name) / "oversized.json"
        target.write_text(" " * (2 * 1024 * 1024 + 1))
        with self.assertRaises(InputError):
            read_json(target)

    def test_unbounded_or_nonfinite_manifest_budget(self):
        for value in [None, float("inf"), -1, True]:
            with self.subTest(value=value):
                self.manifest["budgets"]["max_cost_usd"] = value
                if value == float("inf"):
                    with self.assertRaises(ValueError):
                        self.result()
                else:
                    self.assert_blocked()


if __name__ == "__main__":
    unittest.main()
