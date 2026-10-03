"""Exercise real bootstrap/install processes while killing their fake daemon parent."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/update_fixtures"


class UpdateSurvivalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.install = self.home / ".agentcat/connectors"
        self.install.parent.mkdir()
        self.env = os.environ.copy()
        for key in list(self.env):
            if key.startswith(("AGENTCAT_", "CODEX_", "CLAUDE_")):
                self.env.pop(key)
        self.env.update(HOME=str(self.home), USERPROFILE=str(self.home),
                        APPDATA=str(self.home / "AppData/Roaming"),
                        AGENTCAT_HOME=str(self.home / ".agentcat"),
                        AGENTCAT_CONNECTORS_DIR=str(self.install),
                        AGENTCAT_SURVIVAL_SANDBOX="1", AGENTCAT_AUTO_UPDATE="0",
                        AGENTCAT_ORCA_ACCOUNTS="0", PYTHONPATH=str(FIXTURES))
        self.fakebin = self.root / "bin"
        self.fakebin.mkdir()
        self.env["PATH"] = str(self.fakebin) + os.pathsep + self.env["PATH"]
        if os.name == "posix":
            # The real shell bootstrap asks curl for two release assets.
            self.executable("curl", '''import os, shutil, sys
from pathlib import Path
args = sys.argv[1:]
url = next(a for a in args if a.startswith('https://'))
source = os.environ['SURVIVAL_MANIFEST' if url.endswith('.json') else 'SURVIVAL_ARCHIVE']
shutil.copyfile(source, args[args.index('-o') + 1])
''')
            self.executable("launchctl", '''import os, signal, sys
from pathlib import Path
home = Path(os.environ['HOME'])
command = sys.argv[1]
if command == 'bootout':
    pid = home / 'fake-daemon.pid'
    if pid.exists():
        try: os.killpg(int(pid.read_text()), signal.SIGTERM)
        except ProcessLookupError: pass
elif command == 'bootstrap':
    (home / 'bootstrapped').write_text('ok')
elif command == 'print':
    sys.exit(1)
''')
            # Ensure the bootstrap uses this matrix's Python, including 3.9.
            (self.fakebin / "python3").symlink_to(sys.executable)

    def executable(self, name, source):
        path = self.fakebin / name
        path.write_text(f"#!{sys.executable}\n" + source, encoding="utf-8")
        path.chmod(0o755)

    def command(self, *args):
        result = subprocess.run([sys.executable, *map(str, args)], env=self.env,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def build_current(self):
        source = self.root / "source"
        shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(".git", "__pycache__", ".agentcat-local", "dist"))
        version = self.command(source / "bin/agentcat", "version").strip()
        archive = self.root / "current.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
            for path in source.rglob("*"):
                if path.is_file() and "__pycache__" not in path.parts:
                    package.write(path, str(Path("agentcat-connectors-current") / path.relative_to(source)))
        manifest = dict(version=version, contractVersion=1, sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                        archiveUrl="https://github.com/yong076/agentcat-connectors/releases/download/test/current.zip")
        manifest_path = self.root / "current.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.env.update(SURVIVAL_VERSION=version, SURVIVAL_ARCHIVE=str(archive), SURVIVAL_MANIFEST=str(manifest_path))
        return source, version

    def fake_windows_downloads(self):
        if os.name != "nt":
            return
        script = self.install / "install.ps1"
        # Inject only the download boundary; execute the production PowerShell
        # checksum, extraction and public_channel_install entrypoints unchanged.
        prelude = '''function Invoke-WebRequest {
  param($Uri, $OutFile, [switch]$UseBasicParsing)
  $source = if ($Uri.EndsWith('.json')) { $env:SURVIVAL_MANIFEST } else { $env:SURVIVAL_ARCHIVE }
  Copy-Item -LiteralPath $source -Destination $OutFile
}
'''
        script.write_text(prelude + script.read_text(encoding="utf-8"), encoding="utf-8")

    def update_and_assert_survival(self, version):
        self.fake_windows_downloads()
        marker = self.home / "bootstrapped"
        marker.unlink(missing_ok=True)
        daemon = subprocess.Popen([sys.executable, str(FIXTURES / "fake_daemon.py")], env=self.env,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                  start_new_session=(os.name == "posix"))
        try:
            deadline = time.monotonic() + 90
            log = self.home / ".agentcat/auto-update.out.log"
            while time.monotonic() < deadline:
                text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
                if '"status": "installed"' in text and marker.exists():
                    break
                time.sleep(0.1)
            err = self.home / ".agentcat/auto-update.err.log"
            errors = err.read_text(encoding="utf-8", errors="replace") if err.exists() else ""
            self.assertIn('"status": "installed"', text, errors)
            self.assertIn(f'"version": "{version}"', text)
            self.assertTrue(marker.exists(), "installer did not bootstrap the replacement")
            self.assertNotIn("Terminated", errors)
            self.assertNotEqual(daemon.wait(timeout=5), 0, "fake daemon was not killed")
            self.assertEqual(self.command(self.install / "bin/agentcat", "version").strip(), version)
            snapshot = json.loads(self.command(self.install / "bin/agentcat", "snapshot", "--json"))
            self.assertEqual(snapshot["connectorVersion"], version)
        finally:
            if daemon.poll() is None:
                daemon.kill()
                daemon.wait(timeout=5)
            daemon.stderr.close()
            # A failed assertion must not leave the detached test installer alive.
            pid_file = self.home / "installer.pid"
            if pid_file.exists():
                try:
                    pid = int(pid_file.read_text())
                    if os.name == "posix":
                        os.killpg(pid, signal.SIGTERM)
                    else:
                        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
                except ProcessLookupError:
                    pass

    def test_installer_survives_daemon_process_termination(self):
        source, version = self.build_current()
        shutil.copytree(source, self.install)
        self.update_and_assert_survival(version)

    @unittest.skipUnless(os.environ.get("AGENTCAT_TEST_PREVIOUS_ARCHIVE"), "N-1 release fixture supplied by rehearsal CI step")
    def test_previous_release_updates_to_working_tree(self):
        source, version = self.build_current()
        archive = Path(os.environ["AGENTCAT_TEST_PREVIOUS_ARCHIVE"])
        manifest = Path(os.environ["AGENTCAT_TEST_PREVIOUS_MANIFEST"])
        previous = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertNotEqual(previous["version"], version, "rehearsal needs distinct N-1 and N versions")
        self.env["SURVIVAL_VERSION"] = previous["version"]
        self.command(ROOT / "scripts/public_channel_install.py", "--archive", archive,
                     "--manifest", manifest, "--install-dir", self.install)
        self.assertEqual(self.command(self.install / "bin/agentcat", "version").strip(), previous["version"])
        self.env["SURVIVAL_VERSION"] = version
        self.update_and_assert_survival(version)
