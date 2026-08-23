import hashlib
import importlib.util
import json
import subprocess
import tempfile
import unittest
import zipfile
from importlib.machinery import SourceFileLoader
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
LOADER = SourceFileLoader("build_public_release_module", str(REPO_ROOT / "scripts" / "build_public_release.py"))
SPEC = importlib.util.spec_from_loader("build_public_release_module", LOADER)
release_module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(release_module)


class BuildPublicReleaseTests(unittest.TestCase):
    def test_builds_zip_and_matching_manifest_from_git(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            (source / "bin").mkdir(parents=True)
            (source / "contracts").mkdir()
            (source / "bin" / "agentcat").write_text(
                'CONNECTOR_VERSION = os.environ.get("AGENTCAT_CONNECTOR_VERSION", "26.34.6")\n',
                encoding="utf-8",
            )
            (source / "contracts" / "connector-v1.json").write_text("{}\n", encoding="utf-8")
            subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
            subprocess.run(
                [
                    "git", "-C", str(source),
                    "-c", "user.name=Agent Cat Test",
                    "-c", "user.email=test@agentcat.invalid",
                    "commit", "-m", "fixture",
                ],
                check=True,
                capture_output=True,
            )
            archive, manifest_path = release_module.build_release(
                source,
                Path(temp) / "dist",
                "yong076/agentcat-connectors",
                "HEAD",
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(manifest["version"], "26.34.6")
            self.assertEqual(manifest["contractVersion"], 1)
            self.assertEqual(manifest["sha256"], hashlib.sha256(archive.read_bytes()).hexdigest())
            self.assertTrue(manifest["archiveUrl"].endswith(archive.name))
            with zipfile.ZipFile(archive) as package:
                names = package.namelist()
            self.assertTrue(any(name.endswith("/bin/agentcat") for name in names))
            self.assertTrue(any(name.endswith("/contracts/connector-v1.json") for name in names))


if __name__ == "__main__":
    unittest.main()
