"""Independent bridge adversarial tests: local synthetic Python CLI only."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import unittest

from fastapi.testclient import TestClient
from gradia_bridges.adapter import Bridge, BridgeError, DEFAULT_OPERATIONS, OperationSpec, Principal, Registry, RufloEdition, _run, canonical, sha
from gradia_bridges.api import create_app
import test_bridge as fixture


class AdversarialTests(unittest.TestCase):
    setUp = fixture.BridgeTests.setUp
    request = fixture.BridgeTests.request

    def client(self):
        return TestClient(create_app(self.bridge, {
            "a-private-test-token-long": self.principal,
            "b-private-test-token-long": Principal("b", frozenset({"ruflo:inspect"})),
        }))

    @property
    def headers(self):
        return {"Authorization": "Bearer a-private-test-token-long"}

    def test_nested_argument_shapes_refuse_without_execution(self):
        client = self.client()
        for args in [{"topology": [], "max_agents": 1}, {"topology": "ring", "max_agents": {}}, {"topology": "ring", "max_agents": True}, {"topology": "ring", "max_agents": 5}]:
            response = client.post("/v1/providers/ruflo/execute", headers=self.headers, json=self.request("swarm.init", args))
            self.assertEqual(response.status_code, 400, response.text)

    def test_shell_metacharacters_cannot_become_argv_or_workspace(self):
        marker = self.base / "MUST_NOT_EXIST"
        for topology in [f"ring; touch {marker}", f"$(touch {marker})", "--help", "ring\x00"]:
            with self.assertRaises(BridgeError):
                self.bridge.execute(self.principal, self.request("swarm.init", {"topology": topology, "max_agents": 1}), "attack")
        self.assertFalse(marker.exists())

    def test_json_duplicate_keys_and_large_requests_refuse(self):
        client = self.client()
        response = client.post("/v1/providers/ruflo/execute", headers=self.headers, content='{"edition":"3.56.9","edition":"3.56.9"}')
        self.assertEqual(response.status_code, 400)
        response = client.post("/v1/providers/ruflo/execute", headers=self.headers, content=" " * 4097)
        self.assertEqual(response.status_code, 413)

    def test_deeply_nested_json_is_invalid_input_not_host_failure(self):
        raw = '[' * 1500 + '0' + ']' * 1500
        response = self.client().post("/v1/providers/ruflo/execute", headers=self.headers, content=raw)
        self.assertEqual(response.status_code, 400, response.text)

    def test_reserved_mutation_stays_uncertain_across_restart(self):
        request = self.request("swarm.init", {"topology": "ring", "max_agents": 1})
        with sqlite3.connect(self.bridge.journal) as db:
            db.execute("INSERT INTO executions VALUES (?,?,?,NULL)", ("a", "interrupted", sha(canonical({"provider": "ruflo", "request": request}).encode())))
        restarted = Bridge(Registry([self.edition]), {"a": self.workspace}, self.bridge.journal)
        receipt = restarted.execute(self.principal, request, "interrupted")
        self.assertEqual(receipt["result"], "uncertain")
        self.assertFalse(receipt["retry_safe"])
        self.assertNotIn("output_sha256", receipt)

    def test_capacity_reservation_never_automatically_retries_mutation(self):
        self.bridge.slots.acquire(); self.bridge.slots.acquire()
        self.addCleanup(self.bridge.slots.release); self.addCleanup(self.bridge.slots.release)
        request = self.request("swarm.init", {"topology": "ring", "max_agents": 1})
        with self.assertRaisesRegex(BridgeError, "capacity_exhausted"):
            self.bridge.execute(self.principal, request, "capacity-reserved")
        receipt = self.bridge.execute(self.principal, request, "capacity-reserved")
        self.assertEqual(receipt["result"], "uncertain")
        self.assertFalse(receipt["retry_safe"])

    def test_idempotency_keys_do_not_cross_tenants(self):
        request = self.request("swarm.init", {"topology": "ring", "max_agents": 1})
        first = self.bridge.execute(self.principal, request, "same-key")
        other = Principal("b", self.principal.permissions)
        second = self.bridge.execute(other, request, "same-key")
        self.assertEqual(first["tenant_id"], "a")
        self.assertEqual(second["tenant_id"], "b")
        self.assertNotEqual(first["event_id"], second["event_id"])

    def test_runtime_pin_is_checked_for_read_operations(self):
        self.entry.write_text("print('UNPINNED_PAYLOAD')\n")
        response = self.client().post("/v1/providers/ruflo/execute", headers=self.headers, json=self.request())
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("UNPINNED_PAYLOAD", response.text)

    def test_contract_argument_schema_cannot_silently_drift(self):
        manifest = json.loads(self.manifest.read_text())
        manifest["operations"][2]["arguments"]["max_agents"]["maximum"] = 999
        self.manifest.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(BridgeError, "contract_authority_mismatch"):
            RufloEdition(Path(sys.executable), self.entry, self.manifest).contract()

    def test_even_harmless_manifest_drift_invalidates_constructed_edition(self):
        manifest = json.loads(self.manifest.read_text())
        manifest["displayName"] = "Renamed without changing permissions"
        self.manifest.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(BridgeError, "contract_authority_or_identity_mismatch"):
            self.bridge.catalog("3.56.9")
        # A new host construction pins these bytes and must still verify authority.
        fresh = RufloEdition(Path(sys.executable), self.entry, self.manifest)
        self.assertEqual(fresh.contract()["displayName"], manifest["displayName"])

    def test_new_operation_enforces_declared_arguments_before_builder(self):
        shape = {"count": {"type": "integer", "minimum": 1, "maximum": 3}}
        manifest = json.loads(self.manifest.read_text())
        manifest["operations"].append({"id": "fixture.count", "effect": "inspect", "permission": "ruflo:inspect", "arguments": shape})
        self.manifest.write_text(json.dumps(manifest))
        called = []
        def builder(arguments):
            called.append(arguments)
            return ["--version"]
        edition = RufloEdition(Path(sys.executable), self.entry, self.manifest, DEFAULT_OPERATIONS + (OperationSpec("fixture.count", "ruflo:inspect", "inspect", shape, builder),))
        bridge = Bridge(Registry([edition]), {"a": self.workspace}, self.base / "argument-registry.sqlite")
        request = {"edition": "3.56.9", "operation": "fixture.count", "expected_contract_hash": bridge.catalog("3.56.9")["contractSha256"], "arguments": {"count": -1}}
        with self.assertRaisesRegex(BridgeError, "invalid_arguments"):
            bridge.execute(self.principal, request)
        self.assertEqual(called, [], "Schema violations reached the host argv builder")

    def test_diagnostics_cannot_disclose_body_token_or_other_tenant(self):
        client = self.client()
        marker = "PRIVATE_BODY_DO_NOT_RECORD"
        client.post("/v1/providers/ruflo/execute", headers=self.headers, content=marker)
        diagnostics = client.get("/v1/providers/ruflo/diagnostics", headers=self.headers)
        self.assertNotIn(marker, diagnostics.text)
        self.assertNotIn("a-private-test-token-long", diagnostics.text)
        other = client.get("/v1/providers/ruflo/diagnostics", headers={"Authorization": "Bearer b-private-test-token-long"})
        self.assertEqual(other.json()["events"], [])

    def test_spawned_descendant_cannot_write_after_timeout(self):
        marker = self.base / "child-must-not-finish"
        child = f"import time;from pathlib import Path;time.sleep(.5);Path({str(marker)!r}).write_text('escaped')"
        parent = f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{child!r}]);time.sleep(5)"
        result = _run([sys.executable, "-c", parent], self.workspace, {}, .1)
        self.assertEqual(result[0], "timeout")
        time.sleep(.7)
        self.assertFalse(marker.exists(), "Original process group survived cleanup")

    def test_combined_stdout_stderr_has_one_output_budget(self):
        code = "import os;os.write(1,b'x'*40000);os.write(2,b'y'*40000)"
        result = _run([sys.executable, "-c", code], self.workspace, {}, 1)
        self.assertEqual(result[0], "output_limit")
        self.assertLessEqual(len(result[2]), 65536)

    def test_nested_workspaces_cannot_be_shared_between_tenants(self):
        nested = self.workspace / "nested"
        nested.mkdir()
        with self.assertRaisesRegex(BridgeError, "workspace_overlap"):
            Bridge(Registry([self.edition]), {"a": self.workspace, "b": nested}, self.base / "nested-journal")


if __name__ == "__main__":
    unittest.main()
