import importlib.util
import json
import subprocess
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
LOADER = SourceFileLoader("agentcat_module_contract", str(REPO_ROOT / "bin" / "agentcat"))
SPEC = importlib.util.spec_from_loader("agentcat_module_contract", LOADER)
agentcat = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(agentcat)


def completed(args: list[str], returncode: int) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args, returncode, stdout="", stderr="")


class ConnectorContractTests(unittest.TestCase):
    def setUp(self) -> None:
        agentcat._DAEMON_STATUS_VALUE = None
        agentcat._DAEMON_STATUS_AT = 0.0

    def test_contract_matches_runtime_versions_and_capabilities(self) -> None:
        contract = agentcat.connector_contract_payload()
        capabilities = set(agentcat.CONNECTOR_CAPABILITIES)

        self.assertEqual(contract["contractVersion"], agentcat.CONTRACT_VERSION)
        self.assertEqual(contract["snapshotSchemaVersion"], agentcat.SCHEMA_VERSION)
        self.assertTrue(set(contract["sharedRequired"]).issubset(capabilities))
        for app in contract["apps"].values():
            required = set(app["required"]) | set(app["platformRequired"])
            self.assertTrue(required.issubset(capabilities))
        for old, replacement in contract["compatibilityAliases"].items():
            self.assertIn(old, capabilities)
            self.assertIn(replacement, capabilities)

    def test_version_payload_contract_is_available_to_apps(self) -> None:
        self.assertIn("connector.contract.v1", agentcat.CONNECTOR_CAPABILITIES)
        self.assertIn("connector.daemon", agentcat.CONNECTOR_CAPABILITIES)
        self.assertIn("connector.daemon.status.v1", agentcat.CONNECTOR_CAPABILITIES)

    def test_windows_scheduled_task_has_priority_and_leaks_no_command(self) -> None:
        def run(args: list[str], **_: object) -> subprocess.CompletedProcess:
            if args[0] == "schtasks.exe":
                return completed(args, 0)
            if args[0] == "reg.exe":
                return completed(args, 1)
            raise AssertionError(args)

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(agentcat, "IS_WINDOWS", True), \
                patch.object(agentcat, "WINDOWS_LEGACY_STARTUP_SCRIPT", Path(tmp) / "AgentCatD.vbs"), \
                patch.object(agentcat.subprocess, "run", side_effect=run):
            status = agentcat.daemon_status_snapshot(force=True)

        self.assertEqual(status["registration"], "scheduled_task")
        self.assertEqual(status["status"], "registered")
        self.assertTrue(status["persistent"])
        self.assertNotIn("command", json.dumps(status).lower())
        self.assertNotIn("path", json.dumps(status).lower())

    def test_windows_run_key_is_a_supported_fallback(self) -> None:
        def run(args: list[str], **_: object) -> subprocess.CompletedProcess:
            return completed(args, 0 if args[0] == "reg.exe" else 1)

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(agentcat, "IS_WINDOWS", True), \
                patch.object(agentcat, "WINDOWS_LEGACY_STARTUP_SCRIPT", Path(tmp) / "AgentCatD.vbs"), \
                patch.object(agentcat.subprocess, "run", side_effect=run):
            status = agentcat.daemon_status_snapshot(force=True)

        self.assertEqual(status["registration"], "run_key")
        self.assertEqual(status["status"], "fallback")
        self.assertTrue(status["persistent"])

    def test_windows_missing_registration_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(agentcat, "IS_WINDOWS", True), \
                patch.object(agentcat, "WINDOWS_LEGACY_STARTUP_SCRIPT", Path(tmp) / "AgentCatD.vbs"), \
                patch.object(agentcat.subprocess, "run", side_effect=lambda args, **_: completed(args, 1)):
            status = agentcat.daemon_status_snapshot(force=True)

        self.assertEqual(status["registration"], "none")
        self.assertEqual(status["status"], "missing")
        self.assertFalse(status["persistent"])

    def test_macos_launch_agent_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plist = Path(tmp) / "com.trappist.agentcatd.plist"
            plist.write_text("plist", encoding="utf-8")
            with patch.object(agentcat, "IS_WINDOWS", False), \
                    patch.object(agentcat.sys, "platform", "darwin"), \
                    patch.object(agentcat, "LAUNCHD_AGENT_PLIST", plist):
                status = agentcat.daemon_status_snapshot(force=True)

        self.assertEqual(status["platform"], "macos")
        self.assertEqual(status["registration"], "launch_agent")
        self.assertTrue(status["persistent"])

    def test_golden_fixtures_cover_app_fallback_scenarios(self) -> None:
        fixtures_dir = REPO_ROOT / "contracts" / "fixtures"
        expected = {
            "empty", "healthy", "stale", "partial", "multi-account",
            "update-required", "provider-quota-v2",
        }
        paths = {path.stem: path for path in fixtures_dir.glob("*.json")}
        self.assertEqual(set(paths), expected)

        for name, path in paths.items():
            payload = json.loads(path.read_text(encoding="utf-8"))
            snapshot = payload["snapshot"]
            self.assertEqual(payload["scenario"], name)
            self.assertEqual(snapshot["schemaVersion"], agentcat.SCHEMA_VERSION)
            self.assertEqual(snapshot["contractVersion"], agentcat.CONTRACT_VERSION)
            self.assertIn("connector.contract.v1", snapshot["capabilities"])
            self.assertIn("connector.daemon.status.v1", snapshot["capabilities"])
            self.assertIn("providerInstances.v1", snapshot["capabilities"])
            self.assertIn("quota.scoped.v2", snapshot["capabilities"])
            self.assertIn("providers.metadata.v1", snapshot["capabilities"])
            self.assertIsInstance(snapshot["providers"], dict)
            self.assertIsInstance(snapshot["providerInstances"], list)
            self.assertIsInstance(snapshot["activity"], dict)
            self.assertIsInstance(snapshot["daemon"], dict)

            serialized = json.dumps(payload)
            for forbidden in ("C:\\\\Users\\\\", "/Users/", "accessToken", "refreshToken", "commandLine"):
                self.assertNotIn(forbidden, serialized)

    def test_provider_fixture_covers_scoped_quota_and_metadata_contract(self) -> None:
        path = REPO_ROOT / "contracts" / "fixtures" / "provider-quota-v2.json"
        providers = json.loads(path.read_text(encoding="utf-8"))["snapshot"]["providers"]

        self.assertEqual(set(providers), set(agentcat.CONFIG_PROVIDER_IDS))
        for provider_id, provider in providers.items():
            meta = provider["meta"]
            self.assertEqual(meta, agentcat.provider_metadata(provider_id))
            self.assertEqual(
                set(meta),
                {"displayName", "brandColor", "iconHint", "windows", "sourceHintKey"},
            )
            self.assertNotIn("/", json.dumps(meta))
            for quota in provider["limits"]["quotas"]:
                self.assertIn(quota["scope"], {"account", "model", "surface"})
                self.assertIs(type(quota["aggregate"]), bool)


if __name__ == "__main__":
    unittest.main()
