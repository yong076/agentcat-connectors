import hashlib
import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
LOADER = SourceFileLoader("pro_channel_install", str(REPO_ROOT / "scripts" / "pro_channel_install.py"))
SPEC = importlib.util.spec_from_loader("pro_channel_install", LOADER)
installer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(installer)


def write_archive(path: Path, files: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as tar:
        for name, body in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))


class ProChannelInstallTests(unittest.TestCase):
    def read_events(self, path: Path) -> list[dict]:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_install_pro_archive_verifies_checksum_and_keeps_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "candidate.tgz"
            write_archive(
                archive,
                {
                    "agentcat-connectors-pro/bin/agentcat": b"new",
                    "agentcat-connectors-pro/scripts/install.py": b"install",
                },
            )
            install_dir = root / "connectors"
            (install_dir / "bin").mkdir(parents=True)
            (install_dir / "bin" / "agentcat").write_bytes(b"old")
            manifest = {
                "version": "26.24.0-1",
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            }

            result = installer.install_pro_archive(archive, manifest, install_dir, root / "backups")

            self.assertEqual(result["status"], "installed")
            self.assertEqual((install_dir / "bin" / "agentcat").read_bytes(), b"new")
            self.assertIsNotNone(result["backupPath"])
            self.assertEqual((Path(result["backupPath"]) / "bin" / "agentcat").read_bytes(), b"old")

    def test_install_pro_archive_writes_lifecycle_events_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "candidate.tgz"
            write_archive(
                archive,
                {
                    "agentcat-connectors-pro/bin/agentcat": b"new",
                    "agentcat-connectors-pro/scripts/install.py": b"install",
                },
            )
            event_log = root / "events.jsonl"
            manifest = {
                "version": "26.24.0-1",
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            }

            installer.install_pro_archive(
                archive,
                manifest,
                root / "connectors",
                root / "backups",
                event_log=event_log,
                device_id="test-device",
            )

            events = self.read_events(event_log)
            self.assertEqual([event["event"] for event in events], [
                "pro_connector_swap_started",
                "pro_connector_swap_succeeded",
            ])
            self.assertTrue(all(event["surface"] == "pro_connector" for event in events))
            self.assertTrue(all(event["device_id"] == "test-device" for event in events))
            self.assertEqual(events[0]["properties"]["version"], "26.24.0-1")

    def test_install_pro_archive_writes_rollback_event_when_swap_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "candidate.tgz"
            write_archive(
                archive,
                {
                    "agentcat-connectors-pro/bin/agentcat": b"new",
                    "agentcat-connectors-pro/scripts/install.py": b"install",
                },
            )
            event_log = root / "events.jsonl"
            manifest = {
                "version": "26.24.0-1",
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            }

            with patch.object(installer, "swap_connector", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    installer.install_pro_archive(
                        archive,
                        manifest,
                        root / "connectors",
                        root / "backups",
                        event_log=event_log,
                    )

            events = self.read_events(event_log)
            self.assertEqual([event["event"] for event in events], [
                "pro_connector_swap_started",
                "pro_connector_swap_rolled_back",
            ])
            self.assertEqual(events[-1]["properties"]["reason"], "RuntimeError")

    def test_checksum_mismatch_does_not_touch_existing_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "candidate.tgz"
            write_archive(
                archive,
                {
                    "agentcat-connectors-pro/bin/agentcat": b"new",
                    "agentcat-connectors-pro/scripts/install.py": b"install",
                },
            )
            install_dir = root / "connectors"
            (install_dir / "bin").mkdir(parents=True)
            (install_dir / "bin" / "agentcat").write_bytes(b"old")

            with self.assertRaises(ValueError):
                installer.install_pro_archive(archive, {"sha256": "0" * 64}, install_dir, root / "backups")

            self.assertEqual((install_dir / "bin" / "agentcat").read_bytes(), b"old")
            self.assertFalse((root / "backups").exists())

    def test_rejects_archive_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "bad.tgz"
            write_archive(archive, {"agentcat-connectors-pro/../../escape": b"x"})

            with self.assertRaises(ValueError):
                installer.extract_connector_archive(archive, root / "extract")

    def test_cli_outputs_install_result_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "candidate.tgz"
            write_archive(
                archive,
                {
                    "agentcat-connectors-pro/bin/agentcat": b"new",
                    "agentcat-connectors-pro/scripts/install.py": b"install",
                },
            )
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps({"version": "26.24.0-1", "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}),
                encoding="utf-8",
            )
            result = installer.install_pro_archive(
                archive=archive,
                manifest=installer.read_manifest(manifest),
                install_dir=root / "connectors",
                backup_root=root / "backups",
            )

            self.assertEqual(result["channel"], "pro")
            self.assertEqual(result["status"], "installed")


if __name__ == "__main__":
    unittest.main()
