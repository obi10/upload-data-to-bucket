import hashlib
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from upload_data_to_bucket.backup import BackupEngine
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


if __name__ == "__main__":
    unittest.main()
