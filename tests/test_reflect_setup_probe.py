"""Focused tests for the manual, metadata-only Reflect setup probe."""

import importlib.util
import json
import tempfile
import unittest
from argparse import Namespace
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
LOADER = SourceFileLoader("agentcat_module_reflect_setup_probe", str(REPO_ROOT / "bin" / "agentcat"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
agentcat = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(agentcat)


class ReflectSetupProbeTests(unittest.TestCase):
    def test_probe_returns_allowlist_and_never_executes_or_uses_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".mcp.json").write_text(
                '{"mcpServers":{"missing":{"command":"agentcat-probe-command-do-not-run"},'
                '"duplicate":{"command":"agentcat-probe-command-do-not-run"},'
                '"duplicate":{"command":"agentcat-probe-command-do-not-run"}}}',
                encoding="utf-8",
            )
            skill = root / ".codex" / "skills" / "example" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            (skill.parent / "guide.md").write_text("guide", encoding="utf-8")
            skill.write_text(
                "---\nname: example\ndescription: metadata only\n---\n"
                "See [guide](guide.md) and [missing](missing.md).\n"
                "SECRET_PROMPT_TEXT MUST NOT APPEAR\n",
                encoding="utf-8",
            )
            (root / "AGENTS.md").write_text("private instructions", encoding="utf-8")

            def fail(*args, **kwargs):
                raise AssertionError("setup probe must not execute commands")

            with patch.object(agentcat.subprocess, "run", side_effect=fail), patch.object(
                agentcat.urllib.request, "urlopen", side_effect=AssertionError("network disabled")
            ):
                result = agentcat.reflect_setup_probe(root)

            self.assertTrue(result["execution"] == {"attempted": False, "network": False})
            self.assertEqual(result["mode"], "manual")
            self.assertEqual(result["mcp"]["duplicateKeys"], 1)
            self.assertEqual(result["mcp"]["executablesMissing"], 2)
            self.assertEqual(result["skills"]["validFrontmatter"], 1)
            self.assertEqual(result["skills"]["missingReferences"], 1)
            self.assertEqual(result["agents"]["readable"], 1)
            self.assertNotIn("SECRET_PROMPT_TEXT", json.dumps(result))
            self.assertEqual(set(result), agentcat.REFLECT_SETUP_PROBE_ALLOWED_KEYS)

    def test_missing_agents_is_guidance_and_multiple_agents_are_not_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "AGENTS.md").write_text("root", encoding="utf-8")
            nested = root / "project"
            nested.mkdir()
            result = agentcat.reflect_setup_probe(nested)
            self.assertEqual(result["agents"]["present"], 1)
            self.assertEqual(result["guidanceCodes"], [])

            (nested / "AGENTS.md").write_text("nested", encoding="utf-8")
            result = agentcat.reflect_setup_probe(nested)
            self.assertEqual(result["agents"]["present"], 2)
            self.assertTrue(result["ok"])

            missing = root / "empty"
            missing.mkdir()
            (nested / "AGENTS.md").unlink()
            (root / "AGENTS.md").unlink()
            result = agentcat.reflect_setup_probe(missing)
            self.assertIn("agents_missing", result["guidanceCodes"])
            self.assertTrue(result["ok"])

    def test_manual_cli_surface_uses_json_only_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch("sys.stdout") as stdout:
            root = Path(temp)
            code = agentcat.command_reflect(Namespace(reflect_command="setup-probe", root=str(root), json=True))
            self.assertEqual(code, 0)
            payload = json.loads("".join(call.args[0] for call in stdout.write.call_args_list))
            self.assertEqual(payload["mode"], "manual")
            self.assertFalse(payload["execution"]["attempted"])


if __name__ == "__main__":
    unittest.main()
