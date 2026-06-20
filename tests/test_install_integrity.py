import os
import shutil
import subprocess
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"

BASH = shutil.which("bash")
SHA_TOOL = shutil.which("sha256sum") or shutil.which("shasum")


def _helpers_only() -> str:
    """Return the install.sh prelude up to the end of verify_archive_checksum.

    The full script runs resolve_repo_dir + python3 on source, so the test
    sources only the integrity helpers (config vars + sha256_of +
    verify_archive_checksum) to exercise the swap gate in isolation.
    """
    lines = INSTALL_SH.read_text(encoding="utf-8").splitlines()
    cut = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("install_from_archive()")
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

    def test_no_digest_keeps_current_behavior(self) -> None:
        # Default install (no checksum source) must never be blocked.
        result = self._run({})
        self.assertEqual(result.returncode, 0, result.stderr)

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
