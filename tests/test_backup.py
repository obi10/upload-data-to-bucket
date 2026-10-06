import hashlib
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch
import os
import errno
from dataclasses import replace
from pathlib import Path

from upload_data_to_bucket.backup import BackupEngine, BackupIncomplete
from upload_data_to_bucket.catalog import Catalog
from upload_data_to_bucket.config import Settings
from upload_data_to_bucket.restore import Restorer
from upload_data_to_bucket.store import sha256_file


class FakeStore:
    def __init__(self, settings, root):
        self.settings = settings
        self.root = root

    def name(self, relative):
        return f"{self.settings.prefix}/{relative}"

    def _path(self, name):
        return self.root.joinpath(*name.split("/"))

    def upload_file(self, path, object_name, sha256=None):
        target = self._path(object_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        return "fake-etag"

    def put_bytes(self, object_name, data, overwrite=True):
        target = self._path(object_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return "fake-etag"

    def get_bytes(self, object_name):
        return self._path(object_name).read_bytes()

    def download(self, object_name, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._path(object_name), destination)

    def verified(self, object_name, digest, size):
        path = self._path(object_name)
        return path.is_file() and path.stat().st_size == size and sha256_file(path) == digest


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        runtime = self.root / "runtime"
        self.settings = replace(
            Settings(), runtime_dir=str(runtime), prefix="test",
            bundle_size=4096, large_file_threshold=2048,
            part_size=1024, reserve_free_space=0,
        )
        self.catalog = Catalog(runtime / "catalog.sqlite3")
        self.store = FakeStore(self.settings, self.root / "objects")

    def tearDown(self):
        self.catalog.close()
        self.temp.cleanup()

    def test_deduplicates_and_restores_complete_tree(self):
        duplicate = b"same content" * 20
        (self.source / "a.txt").write_bytes(duplicate)
        (self.source / "nested").mkdir()
        (self.source / "nested" / "b.txt").write_bytes(duplicate)
        (self.source / "empty-dir").mkdir()
        (self.source / "large.bin").write_bytes(b"L" * 3000)

        engine = BackupEngine(self.settings, self.catalog, self.store)
        self.assertEqual(engine.scan_directory(self.source, "snapshot", kind="test"), 0)
        self.assertEqual(len(self.catalog.pending_contents()), 2)
        engine.upload_pending()
        manifest = engine.publish_snapshot("snapshot")

        restored = self.root / "restored"
        result = Restorer(self.settings, self.store).restore(restored, [manifest])
        self.assertEqual(result["archivos_restaurados"], 3)
        self.assertEqual((restored / "a.txt").read_bytes(), duplicate)
        self.assertEqual((restored / "nested" / "b.txt").read_bytes(), duplicate)
        self.assertTrue((restored / "empty-dir").is_dir())
        self.assertEqual((restored / "large.bin").read_bytes(), b"L" * 3000)

    def test_changed_file_is_new_content(self):
        path = self.source / "value.txt"
        path.write_text("one", encoding="utf-8")
        engine = BackupEngine(self.settings, self.catalog, self.store)
        engine.scan_directory(self.source, "first")
        first = self.catalog.snapshot_paths("first")[0]["sha256"]
        path.write_text("two", encoding="utf-8")
        engine.scan_directory(self.source, "second")
        second = self.catalog.snapshot_paths("second")[0]["sha256"]
        self.assertNotEqual(first, second)

    def test_manifest_rejects_missing_upload(self):
        (self.source / "pending.txt").write_text("pending", encoding="utf-8")
        engine = BackupEngine(self.settings, self.catalog, self.store)
        engine.scan_directory(self.source, "pending")
        with self.assertRaises(Exception):
            engine.publish_snapshot("pending")

    def test_changed_member_does_not_block_stable_files_and_resumes(self):
        stable = self.source / "stable.txt"
        changed = self.source / "changed.xlsx"
        stable.write_bytes(b"stable" * 50)
        changed.write_bytes(b"old" * 100)
        engine = BackupEngine(self.settings, self.catalog, self.store)
        engine.scan_directory(self.source, "current")
        old_sha = sha256_file(changed)
        changed.write_bytes(b"new" * 200)
        with self.assertRaisesRegex(BackupIncomplete, "contenidos modificados"):
            engine.upload_pending("current")
        self.assertEqual(self.catalog.content(sha256_file(stable))["status"], "uploaded")
        self.assertEqual(self.catalog.content(old_sha)["status"], "pending")
        self.assertFalse(list(engine.staging.glob("*.partial")))
        with self.assertRaises(BackupIncomplete):
            engine.publish_snapshot("current")
        engine.scan_directory(self.source, "current")
        # The old, unavailable hash must not poison the next attempt.
        self.assertNotIn(old_sha, [row["sha256"] for row in self.catalog.pending_contents("current")])
        engine.upload_pending("current")
        manifest = engine.publish_snapshot("current")
        destination = self.root / "restored"
        Restorer(self.settings, self.store).restore(destination, [manifest])
        self.assertEqual((destination / "changed.xlsx").read_bytes(), changed.read_bytes())
        self.assertEqual((destination / "stable.txt").read_bytes(), stable.read_bytes())

    def test_same_size_changed_hash_is_deferred(self):
        changed = self.source / "changed"
        stable = self.source / "stable"
        changed.write_bytes(b"A" * 200)
        stable.write_bytes(b"B" * 200)
        engine = BackupEngine(self.settings, self.catalog, self.store)
        engine.scan_directory(self.source, "current")
        changed.write_bytes(b"C" * 200)
        with self.assertRaises(BackupIncomplete):
            engine.upload_pending("current")
        self.assertEqual(self.catalog.content(sha256_file(stable))["status"], "uploaded")

    def test_disappeared_source_does_not_block_others(self):
        gone = self.source / "gone"
        stable = self.source / "stable"
        gone.write_bytes(b"A" * 200)
        stable.write_bytes(b"B" * 200)
        engine = BackupEngine(self.settings, self.catalog, self.store)
        engine.scan_directory(self.source, "current")
        gone.unlink()
        with self.assertRaises(BackupIncomplete):
            engine.upload_pending("current")
        self.assertEqual(self.catalog.content(sha256_file(stable))["status"], "uploaded")

    def test_changed_large_source_does_not_block_small_file(self):
        changed = self.source / "large"
        stable = self.source / "stable"
        changed.write_bytes(b"A" * 3000)
        stable.write_bytes(b"B" * 200)
        engine = BackupEngine(self.settings, self.catalog, self.store)
        engine.scan_directory(self.source, "current")
        changed.write_bytes(b"C" * 3000)
        with self.assertRaises(BackupIncomplete):
            engine.upload_pending("current")
        self.assertEqual(self.catalog.content(sha256_file(stable))["status"], "uploaded")

    def test_retry_reuses_unchanged_inventory_hash(self):
        path = self.source / "unchanged"
        path.write_bytes(b"A" * 100)
        engine = BackupEngine(self.settings, self.catalog, self.store)
        engine.scan_directory(self.source, "current")
        with patch("upload_data_to_bucket.backup.sha256_file", side_effect=AssertionError("unnecessary rehash")):
            self.assertEqual(engine.scan_directory(self.source, "current"), 0)

    def test_network_failure_still_propagates(self):
        (self.source / "file").write_bytes(b"data")
        engine = BackupEngine(self.settings, self.catalog, self.store)
        engine.scan_directory(self.source, "current")
        with patch.object(self.store, "upload_file", side_effect=ConnectionError("network")):
            with self.assertRaises(ConnectionError):
                engine.upload_pending("current")
        self.assertEqual(len(self.catalog.pending_contents("current")), 1)

    def test_local_disk_full_is_not_classified_as_source_change(self):
        (self.source / "file").write_bytes(b"data")
        engine = BackupEngine(self.settings, self.catalog, self.store)
        engine.scan_directory(self.source, "current")
        with patch("tarfile.TarFile.addfile", side_effect=OSError(errno.ENOSPC, "full")):
            with self.assertRaises(OSError):
                engine.upload_pending("current")
        self.assertEqual(len(self.catalog.pending_contents("current")), 1)

    def test_scan_errors_do_not_publish_final_completion(self):
        (self.source / "file").write_bytes(b"data")
        engine = BackupEngine(self.settings, self.catalog, self.store)
        self.catalog.set_meta("partial_uploaded", True)
        scan = engine.scan_directory

        def scan_with_error(root, snapshot_id):
            scan(root, snapshot_id)
            self.catalog.add_path(snapshot_id, "denied", "error", error="access denied")
            return 1

        with patch("upload_data_to_bucket.backup.find_device_roots", return_value=[self.source]), patch.object(engine, "scan_directory", side_effect=scan_with_error):
            with self.assertRaises(BackupIncomplete):
                engine.backup()
        self.assertFalse(self.store._path(self.store.name("manifests/latest.json")).exists())
        self.assertEqual(self.catalog.latest_snapshot()["state"], "incomplete")

    @unittest.skipUnless(os.name == "nt", "Windows long path regression")
    def test_windows_long_paths_are_inventoried(self):
        extended = Path("\\\\?\\" + str(self.source.absolute()))
        folder = extended / ("a" * 120) / ("b" * 120)
        folder.mkdir(parents=True)
        (folder / "long.txt").write_bytes(b"long-path")
        engine = BackupEngine(self.settings, self.catalog, self.store)
        self.assertEqual(engine.scan_directory(self.source, "current"), 0)
        files = [row for row in self.catalog.snapshot_paths("current") if row["entry_type"] == "file"]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["sha256"], hashlib.sha256(b"long-path").hexdigest())
        # Explicit cleanup also works on hosts with legacy non-extended path limits.
        (folder / "long.txt").unlink()
        folder.rmdir()
        folder.parent.rmdir()


if __name__ == "__main__":
    unittest.main()
