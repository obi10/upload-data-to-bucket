import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from upload_data_to_bucket.config import Settings
from upload_data_to_bucket.restore import Restorer, RestorePaused


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_entry(path, data, object_name, object_data, member_name=None):
    return {
        "entry_type": "file", "relative_path": path, "size": len(data),
        "sha256": digest(data), "object_name": object_name,
        "object_sha256": digest(object_data), "object_size": len(object_data),
        "member_name": member_name,
    }


class FakeStore:
    def __init__(self, objects, entries):
        self.objects = objects
        self.entries = entries
        self.downloads = []
        self.fail_on = set()

    def get_bytes(self, name):
        return json.dumps({"entries": self.entries}).encode("utf-8")

    def download(self, name, destination, on_chunk=None):
        self.downloads.append(name)
        if name in self.fail_on:
            raise ConnectionError("simulated outage")
        destination.write_bytes(self.objects[name])
        if on_chunk is not None:
            on_chunk(destination.stat().st_size)


class RestoreResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.destination = self.root / "restored"
        self.progress = self.root / "progress.jsonl"
        self.settings = Settings(runtime_dir=str(self.root / "runtime"))

    def test_resume_skips_verified_objects_and_cleans_partial_file(self):
        first, second = b"one", b"two"
        store = FakeStore(
            {"one": first, "two": second},
            [file_entry("a.txt", first, "one", first),
             file_entry("b.txt", second, "two", second)],
        )
        store.fail_on.add("two")
        restorer = Restorer(self.settings, store)
        with self.assertRaises(ConnectionError):
            restorer.restore(self.destination, ["manifest"], resume=True,
                             progress_file=self.progress)
        self.assertEqual((self.destination / "a.txt").read_bytes(), first)
        (self.destination / "b.txt.restoring").write_bytes(b"partial")

        store.downloads.clear()
        store.fail_on.clear()
        result = restorer.restore(self.destination, ["manifest"], resume=True,
                                  progress_file=self.progress)
        self.assertEqual(store.downloads, ["two"])
        self.assertEqual(result["archivos_restaurados"], 2)
        self.assertEqual(result["archivos_ya_verificados"], 1)
        self.assertEqual((self.destination / "b.txt").read_bytes(), second)
        self.assertFalse((self.destination / "b.txt.restoring").exists())
        events = [json.loads(line)["event"] for line in self.progress.read_text(encoding="utf-8").splitlines()]
        self.assertIn("file_verified_existing", events)
        self.assertIn("object_skipped", events)
        self.assertIn("run_completed", events)

    def test_mismatched_existing_file_is_never_overwritten(self):
        data = b"expected"
        store = FakeStore({"one": data}, [file_entry("a.txt", data, "one", data)])
        self.destination.mkdir()
        (self.destination / "a.txt").write_bytes(b"changed")
        with self.assertRaisesRegex(RuntimeError, "Archivo existente distinto"):
            Restorer(self.settings, store).restore(self.destination, ["manifest"], resume=True)
        self.assertEqual((self.destination / "a.txt").read_bytes(), b"changed")
        self.assertEqual(store.downloads, [])

    def test_reuses_valid_cached_bundle_after_interruption(self):
        first, second = b"alpha", b"beta"
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for data in (first, second):
                info = tarfile.TarInfo(f"blobs/{digest(data)}")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        bundle = buffer.getvalue()
        entries = [file_entry("a.txt", first, "bundle", bundle, f"blobs/{digest(first)}"),
                   file_entry("b.txt", second, "bundle", bundle, f"blobs/{digest(second)}")]
        store = FakeStore({"bundle": bundle}, entries)
        self.destination.mkdir()
        (self.destination / "a.txt").write_bytes(first)
        cache = self.destination / ".oci-restore-cache"
        cache.mkdir()
        (cache / f"{digest(bundle)}.tar").write_bytes(bundle)
        result = Restorer(self.settings, store).restore(
            self.destination, ["manifest"], resume=True, progress_file=self.progress)
        self.assertEqual(store.downloads, [])
        self.assertEqual(result["archivos_ya_verificados"], 1)
        self.assertEqual((self.destination / "b.txt").read_bytes(), second)
        self.assertFalse(cache.exists())

    def test_pause_marker_stops_before_next_object(self):
        data = b"one"
        store = FakeStore({"one": data}, [file_entry("a.txt", data, "one", data)])
        pause = self.root / "pause.flag"
        pause.touch()
        with self.assertRaises(RestorePaused):
            Restorer(self.settings, store).restore(
                self.destination, ["manifest"], resume=True,
                progress_file=self.progress, pause_file=pause)
        self.assertEqual(store.downloads, [])
        self.assertIn('"event": "run_paused"', self.progress.read_text(encoding="utf-8"))

    def test_pause_during_download_keeps_partial_cache_and_redownloads_it(self):
        data = b"download in chunks"
        store = FakeStore({"one": data}, [file_entry("a.txt", data, "one", data)])
        pause = self.root / "pause.flag"

        def partial_download(name, destination, on_chunk=None):
            store.downloads.append(name)
            destination.write_bytes(data[:5])
            pause.touch()
            on_chunk(5)

        restorer = Restorer(self.settings, store)
        with patch.object(store, "download", side_effect=partial_download):
            with self.assertRaises(RestorePaused):
                restorer.restore(self.destination, ["manifest"], resume=True,
                                 progress_file=self.progress, pause_file=pause)
        self.assertEqual((self.destination / ".oci-restore-cache" / f"{digest(data)}.blob").read_bytes(), data[:5])
        pause.unlink()
        result = restorer.restore(self.destination, ["manifest"], resume=True,
                                  progress_file=self.progress, pause_file=pause)
        self.assertEqual(result["archivos_restaurados"], 1)
        self.assertEqual(store.downloads, ["one", "one"])
        self.assertEqual((self.destination / "a.txt").read_bytes(), data)


if __name__ == "__main__":
    unittest.main()
