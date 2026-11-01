"""Run the Windows entrypoint only against an injected temporary installer."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
SCRIPT = Path(__file__).resolve().parents[1] / "uninstall.ps1"


@unittest.skipUnless(POWERSHELL, "PowerShell unavailable")
class UninstallPowerShellTests(unittest.TestCase):
    def run_fixture(self, exit_code):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "Unicode 사용자 connector"
            (source / "scripts").mkdir(parents=True)
            evidence = root / "args.json"
            (source / "scripts" / "install.py").write_text(
                "import json,os,sys\nfrom pathlib import Path\n"
                "Path(os.environ['AGENTCAT_TEST_CALLS']).write_text(json.dumps(sys.argv[1:]))\n"
                f"sys.exit({exit_code})\n", encoding="utf-8")
            env = dict(os.environ, HOME=str(root), USERPROFILE=str(root),
                AGENTCAT_CONNECTORS_DIR=str(source), AGENTCAT_PYTHON=sys.executable,
                AGENTCAT_TEST_CALLS=str(evidence))
            result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT)],
                env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
            return result, json.loads(evidence.read_text()), str(source)

    def test_uninstall_uses_local_source_and_preserves_usage(self):
        result, arguments, source = self.run_fixture(0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(arguments, ["--repo-dir", source, "uninstall"])
        self.assertNotIn("--purge", arguments)

    def test_failed_uninstall_returns_failure(self):
        result, arguments, _ = self.run_fixture(7)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(arguments[-1], "uninstall")


if __name__ == "__main__":
    unittest.main()
