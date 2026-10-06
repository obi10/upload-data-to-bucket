import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from upload_data_to_bucket import source_io
from upload_data_to_bucket.source_io import WindowsBackupReader, backup_access, open_source, stat_source


class AccessContextTests(unittest.TestCase):
    def test_context_restores_reader_after_error(self):
        reader = MagicMock()
        with patch.object(source_io, "WindowsBackupReader", return_value=reader):
            with self.assertRaisesRegex(RuntimeError, "failure"):
                with backup_access([Path("source")], enabled=True):
                    self.assertIs(source_io._backend, reader)
                    raise RuntimeError("failure")
        self.assertIsNone(source_io._backend)
        reader.close.assert_called_once()

    def test_missing_privilege_is_logged_and_fails(self):
        reader = MagicMock()
        reader.enable.side_effect = PermissionError("SeBackupPrivilege not assigned")
        with tempfile.TemporaryDirectory() as temp, patch.object(source_io, "WindowsBackupReader", return_value=reader):
            log = Path(temp) / "log.jsonl"
            with self.assertRaises(PermissionError):
                with backup_access([Path(temp)], enabled=True, diagnostic_path=log):
                    self.fail("must not continue with failed privilege")
            self.assertEqual(json.loads(log.read_text())["status"], "error")
        self.assertIsNone(source_io._backend)
        reader.close.assert_called_once()

    def test_diagnostic_failure_restores_privilege(self):
        reader = MagicMock()
        with tempfile.TemporaryDirectory() as temp, patch.object(source_io, "WindowsBackupReader", return_value=reader):
            with self.assertRaises(OSError):
                with backup_access([Path(temp)], enabled=True, diagnostic_path=Path(temp)):
                    self.fail("log write must fail")
        self.assertIsNone(source_io._backend)
        reader.close.assert_called_once()

    def test_disabled_context_uses_ordinary_io(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "plain.bin"
            path.write_bytes(b"ordinary")
            with backup_access([Path(temp)]):
                with open_source(path) as stream:
                    self.assertEqual(stream.read(), b"ordinary")
                self.assertEqual(stat_source(path).st_size, 8)


@unittest.skipUnless(os.name == "nt", "Windows API tests")
class WindowsReaderTests(unittest.TestCase):
    def test_native_read_metadata_and_empty_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "Unicode-á-日本.bin"
            path.write_bytes(b"content\0" * 32)
            (root / "empty").mkdir()
            reader = WindowsBackupReader([root])
            with reader.open(path) as stream:
                self.assertEqual(stream.read(7), b"content")
                stream.seek(0)
                self.assertEqual(stream.read(), path.read_bytes())
            actual = reader.stat(path)
            expected = path.stat()
            self.assertEqual(actual.st_size, expected.st_size)
            self.assertEqual(actual.st_mtime_ns, expected.st_mtime_ns)
            self.assertEqual(dict(reader.listdir(root))["empty"] & 0x10, 0x10)
            self.assertEqual(list(reader.listdir(root / "empty")), [])
            self.assertEqual(len(list(reader.walk(root, lambda error: self.fail(str(error))))), 2)

    def test_reader_rejects_sibling_outside_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            reader = WindowsBackupReader([Path(temp) / "allowed"])
            self.assertFalse(reader.allows(Path(temp) / "allowed-other" / "file"))
            with self.assertRaises(PermissionError):
                reader.open(Path(temp) / "outside")

    def test_directory_pagination(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for i in range(850):
                (root / (str(i).zfill(4) + "x" * 70)).touch()
            reader = WindowsBackupReader([root])
            names = [name for name, _ in reader.listdir(root)]
            self.assertEqual(len(names), 850)
            self.assertEqual(len(set(names)), 850)

    @unittest.skipUnless(os.environ.get("TOSHIBA_BACKUP_PRIVILEGE_TEST") == "1", "Elevated, isolated ACL laboratory only")
    def test_backup_reads_acl_denied_lab_without_acl_changes(self):
        from upload_data_to_bucket.backup import BackupEngine
        from upload_data_to_bucket.catalog import Catalog
        from upload_data_to_bucket.config import Settings
        from upload_data_to_bucket.restore import Restorer
        from test_backup import FakeStore

        identity = json.loads(subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-Command", "[Security.Principal.WindowsIdentity]::GetCurrent().User.Value | ConvertTo-Json"], text=True))
        with tempfile.TemporaryDirectory(prefix="toshiba-acl-lab-") as temp:
            base = Path(temp)
            source = base / "source"
            protected = source / "protected"
            protected.mkdir(parents=True)
            path = protected / "sample.txt"
            data = b"AUTHORIZED LAB DATA ONLY\n"
            path.write_bytes(data)
            settings = Settings(runtime_dir=str(base / "runtime"), reserve_free_space=0)
            catalog = Catalog(base / "runtime" / "catalog.sqlite3")
            store = FakeStore(settings, base / "objects")
            restricted = []
            try:
                # Only newly-created laboratory paths are modified, never the source disk.
                for target in (path, protected):
                    subprocess.run(["icacls.exe", str(target), "/deny", f"*{identity}:(RD)"], check=True, capture_output=True)
                    restricted.append(target)
                with self.assertRaises(PermissionError):
                    path.read_bytes()
                with self.assertRaises(PermissionError):
                    list(os.scandir(protected))
                before = {str(p): subprocess.check_output(["icacls.exe", str(p)]) for p in restricted}
                with backup_access([source], enabled=True):
                    with open_source(path) as stream:
                        self.assertEqual(stream.read(), data)
                    engine = BackupEngine(settings, catalog, store)
                    self.assertEqual(engine.scan_directory(source, "acl-lab"), 0)
                    engine.upload_pending("acl-lab")
                    manifest = engine.publish_snapshot("acl-lab")
                after = {str(p): subprocess.check_output(["icacls.exe", str(p)]) for p in restricted}
                self.assertEqual(before, after)
                restored = base / "restored"
                Restorer(settings, store).restore(restored, [manifest])
                self.assertEqual((restored / "protected" / "sample.txt").read_bytes(), data)
                self.assertIsNone(source_io._backend)
            finally:
                catalog.close()
                # Remove only the explicit deny entries this test created.
                for target in reversed(restricted):
                    subprocess.run(["icacls.exe", str(target), "/remove:d", f"*{identity}"], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
