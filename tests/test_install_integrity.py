import os
import shutil
import subprocess
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
import tarfile


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"

BASH = shutil.which("bash")
SHA_TOOL = shutil.which("sha256sum") or shutil.which("shasum")


def _helpers_only() -> str:
    """Return install.sh without the entrypoint.

    The full script runs resolve_repo_dir + python3 on source. Tests source the
    function prelude only, then call the helper under test.
    """
    lines = INSTALL_SH.read_text(encoding="utf-8").splitlines()
    cut = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("REPO_DIR=")
    )
    return "\n".join(lines[:cut])


@unittest.skipUnless(
    os.name == "posix" and BASH and SHA_TOOL,
    "install.sh integrity gate is exercised on POSIX shells with a sha256 tool",
)
class InstallShChecksumGateTests(unittest.TestCase):
    """Public auto-update integrity gate in install.sh."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.archive = root / "agentcat-connectors.tar.gz"
        self.archive.write_bytes(b"connector-archive-bytes")
        self.digest = sha256(self.archive.read_bytes()).hexdigest()
        self.helpers = root / "helpers.sh"
        self.helpers.write_text(_helpers_only(), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, env_extra: dict[str, str]) -> subprocess.CompletedProcess:
        script = f'source "{self.helpers}"; verify_archive_checksum "{self.archive}"'
        return subprocess.run(
            [BASH, "-c", script],
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
                "HOME": self.tmp.name,
                **env_extra,
            },
            capture_output=True,
            text=True,
        )

    def _run_script(self, script: str, env_extra: dict[str, str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [BASH, "-c", f'source "{self.helpers}"; {script}'],
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
                "HOME": self.tmp.name,
                **env_extra,
            },
            capture_output=True,
            text=True,
        )

    def test_no_digest_keeps_current_behavior(self) -> None:
        # Default install (no checksum source) must never be blocked.
        result = self._run({})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_archive_url_can_be_pinned_by_caller(self) -> None:
        pinned = "https://github.com/yong076/agentcat-connectors/releases/download/v26.26.0/agentcat-connectors-v26.26.0.tar.gz"
        result = self._run_script(
            'printf "%s" "${ARCHIVE_URL}"',
            {"AGENTCAT_CONNECTORS_ARCHIVE_URL": pinned},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, pinned)

    def test_pinned_archive_url_installs_archive_before_git_clone(self) -> None:
        root = Path(self.tmp.name)
        payload = root / "payload"
        release_root = payload / "agentcat-connectors-v26.26.0"
        (release_root / "scripts").mkdir(parents=True)
        (release_root / "bin").mkdir()
        (release_root / "scripts" / "install.py").write_text("print('ok')\n", encoding="utf-8")
        (release_root / "bin" / "agentcat").write_text("#!/usr/bin/env python3\n", encoding="utf-8")

        archive = root / "release.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(release_root, arcname=release_root.name)

        fakebin = root / "fakebin"
        fakebin.mkdir()
        git_called = root / "git-called"
        (fakebin / "git").write_text(
            f"#!/usr/bin/env bash\nprintf called > {git_called}\nexit 99\n",
            encoding="utf-8",
        )
        (fakebin / "curl").write_text(
            """#!/usr/bin/env bash
set -euo pipefail
out=""
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "-o" ]]; then out="$2"; shift 2; continue; fi
  shift
done
cp "$FAKE_ARCHIVE" "$out"
""",
            encoding="utf-8",
        )
        os.chmod(fakebin / "git", 0o755)
        os.chmod(fakebin / "curl", 0o755)

        install_dir = root / "install"
        result = self._run_script(
            'resolve_repo_dir',
            {
                "AGENTCAT_CONNECTORS_ARCHIVE_URL": "https://example.test/agentcat-connectors-v26.26.0.tar.gz",
                "AGENTCAT_CONNECTORS_DIR": str(install_dir),
                "FAKE_ARCHIVE": str(archive),
                "PATH": f"{fakebin}:{os.environ.get('PATH', '/usr/bin:/bin:/usr/local/bin')}",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(install_dir))
        self.assertTrue((install_dir / "scripts" / "install.py").exists())
        self.assertTrue((install_dir / "bin" / "agentcat").exists())
        self.assertFalse(git_called.exists(), "pinned archive install must not git clone main")

    def test_matching_digest_passes(self) -> None:
        result = self._run({"AGENTCAT_CONNECTORS_SHA256": self.digest})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_mismatched_digest_refuses_swap(self) -> None:
        result = self._run({"AGENTCAT_CONNECTORS_SHA256": "a" * 64})
        self.assertEqual(result.returncode, 1)
        self.assertIn("checksum mismatch", result.stderr)

    def test_malformed_digest_refuses_swap(self) -> None:
        result = self._run({"AGENTCAT_CONNECTORS_SHA256": "NOT-HEX"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("64 lowercase hex", result.stderr)


if __name__ == "__main__":
    unittest.main()
