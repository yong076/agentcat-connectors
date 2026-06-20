import importlib.util
import json
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
LOADER = SourceFileLoader("agentcat_install_module", str(REPO_ROOT / "scripts" / "install.py"))
SPEC = importlib.util.spec_from_loader("agentcat_install_module", LOADER)
install = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(install)


class AgentCatInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_paths = {
            "HOME": install.HOME,
            "AGENTCAT_HOME": install.AGENTCAT_HOME,
            "BACKUPS_DIR": install.BACKUPS_DIR,
            "GEMINI_TELEMETRY": install.GEMINI_TELEMETRY,
            "ANTIGRAVITY_TELEMETRY": install.ANTIGRAVITY_TELEMETRY,
        }
        install.HOME = self.root / "home"
        install.AGENTCAT_HOME = self.root / "agentcat"
        install.BACKUPS_DIR = install.AGENTCAT_HOME / "backups"
        install.GEMINI_TELEMETRY = install.AGENTCAT_HOME / "gemini" / "telemetry.log"
        install.ANTIGRAVITY_TELEMETRY = install.AGENTCAT_HOME / "gemini" / "antigravity-telemetry.log"
        install.HOME.mkdir()
        install.BACKUPS_DIR.mkdir(parents=True)

    def tearDown(self) -> None:
        for name, value in self.old_paths.items():
            setattr(install, name, value)
        self.tmp.cleanup()

    def test_install_gemini_settings_configures_gemini_only_not_antigravity(self) -> None:
        # Antigravity bills server-side and ignores the local telemetry sub-keys;
        # the install must only write a telemetry block for the real gemini-cli and
        # must never touch antigravity-cli/settings.json.
        backup_dir = install.BACKUPS_DIR / "test"
        backup_dir.mkdir(parents=True)

        install.install_gemini_settings(backup_dir)

        gemini_settings = json.loads((install.HOME / ".gemini" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(gemini_settings["telemetry"]["outfile"], str(install.GEMINI_TELEMETRY))
        self.assertFalse(gemini_settings["telemetry"]["logPrompts"])

        antigravity_path = install.HOME / ".gemini" / "antigravity-cli" / "settings.json"
        self.assertFalse(antigravity_path.exists())

    def test_remove_gemini_settings_cleans_prior_antigravity_cli_telemetry(self) -> None:
        # A prior install version may have written an Agent Cat telemetry block into
        # antigravity-cli/settings.json; uninstall must still clean it up.
        backup_dir = install.BACKUPS_DIR / "test"
        backup_dir.mkdir(parents=True)
        antigravity_path = install.HOME / ".gemini" / "antigravity-cli" / "settings.json"
        antigravity_path.parent.mkdir(parents=True, exist_ok=True)
        antigravity_path.write_text(
            json.dumps(
                {
                    "telemetry": {
                        "enabled": True,
                        "target": "local",
                        "outfile": str(install.ANTIGRAVITY_TELEMETRY),
                        "logPrompts": False,
                    }
                }
            ),
            encoding="utf-8",
        )
        install.install_gemini_settings(backup_dir)

        install.remove_agentcat_gemini_settings(backup_dir)

        gemini_settings = json.loads((install.HOME / ".gemini" / "settings.json").read_text(encoding="utf-8"))
        antigravity_settings = json.loads(antigravity_path.read_text(encoding="utf-8"))
        self.assertNotIn("telemetry", gemini_settings)
        self.assertNotIn("telemetry", antigravity_settings)


if __name__ == "__main__":
    unittest.main()
