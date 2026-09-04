import hashlib
import importlib.util
import json
import subprocess
import tempfile
import unittest
import zipfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
LOADER = SourceFileLoader(
    "public_channel_install_module", str(REPO_ROOT / "scripts" / "public_channel_install.py")
)
SPEC = importlib.util.spec_from_loader("public_channel_install_module", LOADER)
installer_module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(installer_module)

CONNECTOR_LOADER = SourceFileLoader(
    "public_channel_connector_module", str(REPO_ROOT / "bin" / "agentcat")
)
CONNECTOR_SPEC = importlib.util.spec_from_loader("public_channel_connector_module", CONNECTOR_LOADER)
connector_module = importlib.util.module_from_spec(CONNECTOR_SPEC)
assert CONNECTOR_SPEC.loader is not None
CONNECTOR_SPEC.loader.exec_module(connector_module)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PublicChannelInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.install_dir = self.root / "home" / ".agentcat" / "connectors"
        self.backup_root = self.root / "home" / ".agentcat" / "backups" / "connector-source"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_archive(self, version: str = "26.34.6") -> tuple[Path, dict]:
        source = self.root / f"agentcat-connectors-{version}"
        (source / "bin").mkdir(parents=True)
        (source / "scripts").mkdir()
        (source / "contracts").mkdir()
        (source / "bin" / "agentcat").write_text("connector", encoding="utf-8")
        (source / "scripts" / "install.py").write_text("installer", encoding="utf-8")
        (source / "contracts" / "connector-v1.json").write_text(
            json.dumps({"contractVersion": 2, "snapshotSchemaVersion": 4}),
            encoding="utf-8",
        )
        archive = self.root / f"connector-{version}.zip"
        with zipfile.ZipFile(archive, "w") as package:
            for path in source.rglob("*"):
                if path.is_file():
                    package.write(path, Path(source.name) / path.relative_to(source))
        manifest = {"version": version, "contractVersion": 2, "sha256": sha256(archive)}
        return archive, manifest

    def test_checksum_mismatch_never_touches_existing_install(self) -> None:
        self.install_dir.mkdir(parents=True)
        marker = self.install_dir / "old.txt"
        marker.write_text("keep", encoding="utf-8")
        archive, manifest = self.make_archive()
        manifest["sha256"] = "0" * 64

        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            installer_module.install_public_archive(
                archive,
                manifest,
                self.install_dir,
                self.backup_root,
                candidate_validator=lambda *_: None,
                installer=lambda *_: None,
                live_validator=lambda *_: None,
            )

        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertFalse(self.backup_root.exists())

    def test_legacy_nonpublic_state_resolves_to_public(self) -> None:
        state_path = self.root / "update-channel.json"
        state_path.write_text(
            json.dumps(
                {
                    "channel": "pro",
                    "status": "manifest_ready",
                    "installStatus": "pending_install",
                    "manifest": {"version": "99.0.0"},
                }
            ),
            encoding="utf-8",
        )

        with mock.patch.object(connector_module, "AGENTCAT_HOME", self.root):
            status = connector_module.update_channel_status_snapshot()

        self.assertEqual(status["channel"], "public")
        self.assertEqual(status["installStatus"], "current")
        self.assertNotIn("targetVersion", status)

    def test_dirty_checkout_is_preserved_and_replacement_is_refused(self) -> None:
        self.install_dir.mkdir(parents=True)
        tracked = self.install_dir / "tracked.txt"
        tracked.write_text("before", encoding="utf-8")
        subprocess.run(["git", "init", str(self.install_dir)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.install_dir), "add", "tracked.txt"], check=True)
        subprocess.run(
            [
                "git", "-C", str(self.install_dir),
                "-c", "user.name=Agent Cat Test",
                "-c", "user.email=test@agentcat.invalid",
                "commit", "-m", "baseline",
            ],
            check=True,
            capture_output=True,
        )
        tracked.write_text("after", encoding="utf-8")
        staged = self.install_dir / "staged.txt"
        staged.write_text("staged", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.install_dir), "add", "staged.txt"], check=True)
        (self.install_dir / "local.bak").write_text("backup", encoding="utf-8")
        archive, manifest = self.make_archive()

        with self.assertRaises(installer_module.DirtyCheckoutError) as raised:
            installer_module.install_public_archive(
                archive,
                manifest,
                self.install_dir,
                self.backup_root,
                candidate_validator=lambda *_: None,
                installer=lambda *_: None,
                live_validator=lambda *_: None,
            )

        recovery = raised.exception.recovery_path
        self.assertEqual(tracked.read_text(encoding="utf-8"), "after")
        self.assertEqual((self.install_dir / "local.bak").read_text(encoding="utf-8"), "backup")
        patch_text = (recovery / "tracked.patch").read_text(encoding="utf-8")
        self.assertIn("tracked.txt", patch_text)
        self.assertIn("staged.txt", patch_text)
        self.assertEqual((recovery / "untracked" / "local.bak").read_text(encoding="utf-8"), "backup")

    def test_successful_install_keeps_previous_source_as_rollback(self) -> None:
        self.install_dir.mkdir(parents=True)
        (self.install_dir / "old.txt").write_text("old", encoding="utf-8")
        archive, manifest = self.make_archive()
        calls: list[str] = []

        result = installer_module.install_public_archive(
            archive,
            manifest,
            self.install_dir,
            self.backup_root,
            candidate_validator=lambda *_: calls.append("candidate"),
            installer=lambda *_: calls.append("install"),
            live_validator=lambda *_: calls.append("live"),
        )

        self.assertEqual(calls, ["candidate", "install", "live"])
        self.assertEqual(result["status"], "installed")
        self.assertTrue((self.install_dir / "bin" / "agentcat").is_file())
        rollback_path = Path(result["rollbackPath"])
        self.assertEqual((rollback_path / "old.txt").read_text(encoding="utf-8"), "old")

    def test_failed_health_validation_restores_previous_source(self) -> None:
        self.install_dir.mkdir(parents=True)
        (self.install_dir / "old.txt").write_text("old", encoding="utf-8")
        archive, manifest = self.make_archive()
        install_calls: list[Path] = []

        with self.assertRaisesRegex(RuntimeError, "health failed"):
            installer_module.install_public_archive(
                archive,
                manifest,
                self.install_dir,
                self.backup_root,
                candidate_validator=lambda *_: None,
                installer=lambda path: install_calls.append(path),
                live_validator=lambda *_: (_ for _ in ()).throw(RuntimeError("health failed")),
            )

        self.assertEqual(len(install_calls), 2)
        self.assertEqual((self.install_dir / "old.txt").read_text(encoding="utf-8"), "old")
        self.assertFalse((self.install_dir / "bin" / "agentcat").exists())

    def test_archive_path_traversal_is_rejected(self) -> None:
        archive = self.root / "unsafe.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("../escape.txt", "no")

        with self.assertRaisesRegex(ValueError, "unsafe archive path"):
            installer_module.extract_archive(archive, self.root / "extract")
        self.assertFalse((self.root / "escape.txt").exists())

    def test_current_candidate_passes_real_contract_validation(self) -> None:
        manifest = {
            "version": "26.34.7",
            "contractVersion": 2,
            "sha256": "0" * 64,
        }
        installer_module.validate_candidate(REPO_ROOT, manifest)

    def test_live_validation_uses_bounded_version_endpoint(self) -> None:
        urls: list[str] = []

        class Response:
            def __init__(self, body: bytes) -> None:
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return self.body

        def fake_urlopen(url, timeout):
            urls.append(url)
            if url.endswith("/healthz"):
                return Response(b"ok")
            return Response(b'{"connectorVersion":"26.34.7","contractVersion":2}')

        with mock.patch.object(installer_module.urllib.request, "urlopen", side_effect=fake_urlopen):
            installer_module.validate_live_connector("26.34.7", 2, timeout=0.1)

        self.assertEqual(
            urls,
            ["http://127.0.0.1:8765/healthz", "http://127.0.0.1:8765/v1/version"],
        )
        self.assertFalse(any(url.endswith("/v1/snapshot") for url in urls))


if __name__ == "__main__":
    unittest.main()
