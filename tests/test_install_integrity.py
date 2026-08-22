import os
import shutil
import subprocess
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"
INSTALL_TEXT = INSTALL_SH.read_text(encoding="utf-8")
BASH = shutil.which("bash")
SHA_TOOL = shutil.which("sha256sum") or shutil.which("shasum")


def _helpers_only() -> str:
    lines = INSTALL_TEXT.splitlines()
    cut = next(index for index, line in enumerate(lines) if line.startswith("script_dir="))
    return "\n".join(lines[:cut])


class InstallShPolicyTests(unittest.TestCase):
    def test_default_uses_release_manifest_not_mutable_main_archive(self) -> None:
        self.assertIn("releases/latest/download/connector-manifest.json", INSTALL_TEXT)
        self.assertNotIn("archive/refs/heads/main", INSTALL_TEXT)

    def test_bootstrap_never_recursively_deletes_install_dir(self) -> None:
        self.assertNotIn('rm -rf "${INSTALL_DIR}"', INSTALL_TEXT)
        self.assertNotIn('git -C "${INSTALL_DIR}" checkout', INSTALL_TEXT)
        self.assertIn("public_channel_install.py", INSTALL_TEXT)

    def test_pinned_archive_requires_digest_and_version(self) -> None:
        self.assertIn("AGENTCAT_CONNECTORS_SHA256", INSTALL_TEXT)
        self.assertIn("AGENTCAT_CONNECTORS_VERSION", INSTALL_TEXT)


@unittest.skipUnless(
    os.name == "posix" and BASH and SHA_TOOL,
    "install.sh checksum gate is exercised on POSIX shells with a sha256 tool",
)
class InstallShChecksumGateTests(unittest.TestCase):
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

    def _run(self, expected: str) -> subprocess.CompletedProcess:
        script = f'source "{self.helpers}"; verify_archive_checksum "{self.archive}" "{expected}"'
        return subprocess.run(
            [BASH, "-c", script],
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
                "HOME": self.tmp.name,
            },
            capture_output=True,
            text=True,
        )

    def test_matching_digest_passes(self) -> None:
        result = self._run(self.digest)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_mismatched_digest_refuses_install(self) -> None:
        result = self._run("a" * 64)
        self.assertEqual(result.returncode, 1)
        self.assertIn("checksum mismatch", result.stderr)

    def test_malformed_digest_refuses_install(self) -> None:
        result = self._run("NOT-HEX")
        self.assertEqual(result.returncode, 1)
        self.assertIn("64 lowercase hex", result.stderr)


if __name__ == "__main__":
    unittest.main()
