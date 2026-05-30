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

    def test_install_gemini_settings_configures_antigravity_cli_telemetry(self) -> None:
        backup_dir = install.BACKUPS_DIR / "test"
        backup_dir.mkdir(parents=True)

        install.install_gemini_settings(backup_dir)

        gemini_settings = json.loads((install.HOME / ".gemini" / "settings.json").read_text(encoding="utf-8"))
        antigravity_settings = json.loads(
            (install.HOME / ".gemini" / "antigravity-cli" / "settings.json").read_text(encoding="utf-8")
        )

        self.assertEqual(gemini_settings["telemetry"]["outfile"], str(install.GEMINI_TELEMETRY))
        self.assertEqual(antigravity_settings["telemetry"]["outfile"], str(install.ANTIGRAVITY_TELEMETRY))
        self.assertFalse(gemini_settings["telemetry"]["logPrompts"])
        self.assertFalse(antigravity_settings["telemetry"]["logPrompts"])

    def test_remove_gemini_settings_cleans_antigravity_cli_telemetry(self) -> None:
        backup_dir = install.BACKUPS_DIR / "test"
        backup_dir.mkdir(parents=True)
        install.install_gemini_settings(backup_dir)

        install.remove_agentcat_gemini_settings(backup_dir)

        gemini_settings = json.loads((install.HOME / ".gemini" / "settings.json").read_text(encoding="utf-8"))
        antigravity_settings = json.loads(
            (install.HOME / ".gemini" / "antigravity-cli" / "settings.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("telemetry", gemini_settings)
        self.assertNotIn("telemetry", antigravity_settings)


if __name__ == "__main__":
    unittest.main()
