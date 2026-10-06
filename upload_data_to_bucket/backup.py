from __future__ import annotations

import gzip
import errno
import hashlib
import json
import os
import shutil
import stat
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from .catalog import Catalog, utc_now
from .config import Settings
from .device import find_device_roots
from .store import OciStore, sha256_file


class DeviceNotFound(RuntimeError):
    pass


class BackupIncomplete(RuntimeError):
    pass


class SourceUnavailable(BackupIncomplete):
    def __init__(self, row, reason):
        self.sha256 = row["sha256"]
        self.source_path = row["source_path"]
        super().__init__(f"{self.source_path}: {reason}")


def safe_relative(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Ruta relativa inválida: {value}")
    return path.as_posix()


class HashingReader:
    def __init__(self, stream):
        self.stream = stream
        self.digest = hashlib.sha256()

    def read(self, size=-1):
        data = self.stream.read(size)
        self.digest.update(data)
        return data


class BackupEngine:
    def __init__(self, settings: Settings, catalog: Catalog, store: OciStore):
        self.settings = settings
        self.catalog = catalog
        self.store = store
        self.staging = settings.runtime_path / "staging"
        self.staging.mkdir(parents=True, exist_ok=True)

    def import_partial(self) -> str:
        snapshot_id = "partial-20260924"
        root = self.settings.partial_path
        content = root / "contenido"
        audit = root / "auditoria.jsonl"
        if self.catalog.get_meta("partial_uploaded", False):
            return snapshot_id
        if not content.is_dir() or not audit.is_file():
            raise FileNotFoundError(f"No existe la copia parcial esperada: {root}")
        self.catalog.create_snapshot(snapshot_id, "partial", str(content))
        with audit.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                try:
                    record = json.loads(line)
                    relative = safe_relative(str(record["archivo"]))
                    result = record.get("resultado")
                    if result == "carpeta_creada":
                        self.catalog.add_path(snapshot_id, relative, "directory")
                    elif result == "copiado_y_verificado":
                        source = content / Path(relative)
                        size = int(record["bytes"])
                        digest = str(record["sha256"]).lower()
                        info = source.stat()
                        if not source.is_file() or info.st_size != size:
                            raise OSError("La copia parcial no coincide con su auditoría")
                        self.catalog.upsert_content(digest, size, str(source))
                        self.catalog.add_path(snapshot_id, relative, "file", digest, size, info.st_mtime_ns)
                except Exception as error:
                    self.catalog.add_path(
                        snapshot_id, f"__audit_error__/{line_number}", "error", error=str(error)
                    )
        self.catalog.set_snapshot_state(snapshot_id, "scanned")
        return snapshot_id

    def scan_directory(self, root: Path, snapshot_id: str, kind="external") -> int:
        # Extended Windows paths also allow inventorying names over MAX_PATH.
        if os.name == "nt" and not str(root).startswith("\\\\?\\"):
            absolute = str(root.absolute())
            root = Path("\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\") else "\\\\?\\" + absolute)
        root = root.resolve(strict=True)
        self.catalog.create_snapshot(snapshot_id, kind, str(root))
        previous = {row["relative_path"]: row for row in self.catalog.snapshot_paths(snapshot_id)
                    if row["entry_type"] == "file"}
        self.catalog.clear_snapshot_paths(snapshot_id)
        self.catalog.set_snapshot_state(snapshot_id, "scanning", errors=0)
        errors = 0

        def walk_error(error):
            nonlocal errors
            errors += 1
            try:
                relative = str(Path(error.filename).relative_to(root)).replace("\\", "/")
            except Exception:
                relative = f"__walk_error__/{errors}"
            self.catalog.add_path(snapshot_id, relative, "error", error=str(error))

        for current, directories, filenames in os.walk(root, topdown=True, followlinks=False, onerror=walk_error):
            current_path = Path(current)
            directories.sort(key=str.casefold)
            filenames.sort(key=str.casefold)
            for name in list(directories):
                path = current_path / name
                try:
                    info = path.lstat()
                    if stat.S_ISLNK(info.st_mode) or (
                        getattr(info, "st_file_attributes", 0)
                        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                    ):
                        directories.remove(name)
                        raise ValueError("Reparse point omitido")
                    relative = path.relative_to(root).as_posix()
                    self.catalog.add_path(snapshot_id, relative, "directory", mtime_ns=info.st_mtime_ns)
                except Exception as error:
                    errors += 1
                    self.catalog.add_path(snapshot_id, path.relative_to(root).as_posix(), "error", error=str(error))
            for name in filenames:
                path = current_path / name
                relative = path.relative_to(root).as_posix()
                try:
                    before = path.stat()
                    if not stat.S_ISREG(before.st_mode):
                        raise ValueError("No es un archivo regular")
                    cached = previous.get(relative)
                    # Reuse known hashes only when size and nanosecond mtime match.
                    if (cached is not None and cached["sha256"]
                            and cached["size"] == before.st_size and cached["mtime_ns"] == before.st_mtime_ns
                            and self.catalog.content(cached["sha256"]) is not None):
                        digest = cached["sha256"]
                    else:
                        digest = sha256_file(path)
                    after = path.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise OSError("El archivo cambió durante el inventario")
                    self.catalog.upsert_content(digest, before.st_size, str(path))
                    self.catalog.add_path(
                        snapshot_id, relative, "file", digest, before.st_size, before.st_mtime_ns
                    )
                except Exception as error:
                    errors += 1
                    self.catalog.add_path(snapshot_id, relative, "error", error=str(error))
        self.catalog.set_snapshot_state(snapshot_id, "scanned", errors=errors)
        return errors

    def upload_pending(self, snapshot_id: str | None = None) -> None:
        pending = list(self.catalog.pending_contents(snapshot_id))
        failures = []

        def defer(error):
            record = {"timestamp": utc_now(), "snapshot": snapshot_id,
                      "sha256": error.sha256, "source": error.source_path, "error": str(error)}
            failures.append(record)
            with (self.settings.runtime_path / "upload-errors.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")

        def upload_group(rows):
            # If a source changes while building the TAR, rebuild without that
            # member. Confirm all stable members and leave the failed hash pending.
            remaining = list(rows)
            while remaining:
                try:
                    self._upload_bundle(remaining)
                    return
                except SourceUnavailable as error:
                    defer(error)
                    remaining = [row for row in remaining if row["sha256"] != error.sha256]

        large = [row for row in pending if row["size"] > self.settings.large_file_threshold]
        small = [row for row in pending if row["size"] <= self.settings.large_file_threshold]
        for row in large:
            path = Path(row["source_path"])
            try:
                if sha256_file(path) != row["sha256"]:
                    raise SourceUnavailable(row, "Cambió el archivo")
            except (OSError, SourceUnavailable) as error:
                defer(error if isinstance(error, SourceUnavailable) else SourceUnavailable(row, error))
                continue
            object_name = self.store.name(f"blobs/files/{row['sha256']}")
            etag = self.store.upload_file(path, object_name, row["sha256"])
            self.catalog.mark_content_uploaded(
                row["sha256"], object_name, None, row["sha256"], row["size"], etag
            )

        group: list = []
        total = 0
        for row in small:
            if group and total + row["size"] > self.settings.bundle_size:
                upload_group(group)
                group, total = [], 0
            group.append(row)
            total += row["size"]
        if group:
            upload_group(group)
        self.catalog.set_meta("last_upload_errors", failures)
        if failures:
            raise BackupIncomplete(f"Se subieron los contenidos estables; quedan {len(failures)} contenidos modificados o inaccesibles para reintentar")

    def _add_bundle_member(self, archive, row):
        path = Path(row["source_path"])
        try:
            before = path.stat()
            if before.st_size != row["size"]:
                raise SourceUnavailable(row, "Cambió el tamaño")
            info = tarfile.TarInfo(f"blobs/{row['sha256']}")
            info.size = before.st_size
            info.mtime = before.st_mtime
            info.mode = stat.S_IMODE(before.st_mode)
            with path.open("rb") as source:
                reader = HashingReader(source)
                archive.addfile(info, reader)
            after = path.stat()
            if reader.digest.hexdigest() != row["sha256"]:
                raise SourceUnavailable(row, "Cambió SHA-256")
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise SourceUnavailable(row, "Cambió durante la creación del TAR")
        except OSError as error:
            if error.errno in (errno.ENOSPC, errno.EDQUOT, errno.EIO):
                raise
            raise SourceUnavailable(row, error) from error

    def _upload_bundle(self, rows) -> None:
        identity = hashlib.sha256()
        for row in rows:
            identity.update(row["sha256"].encode("ascii"))
            identity.update(str(row["size"]).encode("ascii"))
        bundle_key = identity.hexdigest()
        tar_path = self.staging / f"bundle-{bundle_key}.tar"
        partial_path = tar_path.with_suffix(".tar.partial")
        required = min(self.settings.bundle_size, sum(row["size"] for row in rows) + 16 * 1024**2)
        free = shutil.disk_usage(self.staging).free
        if free - required < self.settings.reserve_free_space:
            raise OSError(
                f"Espacio libre insuficiente para el TAR: {free} bytes libres; "
                f"se reservan {self.settings.reserve_free_space}"
            )
        if not tar_path.exists():
            partial_path.unlink(missing_ok=True)
            try:
                with tarfile.open(partial_path, "w", format=tarfile.PAX_FORMAT) as archive:
                    for row in rows:
                        self._add_bundle_member(archive, row)
            except Exception:
                partial_path.unlink(missing_ok=True)
                raise
            partial_path.replace(tar_path)
        archive_sha = sha256_file(tar_path)
        object_name = self.store.name(f"blobs/bundles/{archive_sha}.tar")
        etag = self.store.upload_file(tar_path, object_name, archive_sha)
        bundle_manifest = {
            "schema": 1,
            "object": object_name,
            "sha256": archive_sha,
            "size": tar_path.stat().st_size,
            "members": [
                {"sha256": row["sha256"], "size": row["size"], "member": f"blobs/{row['sha256']}"}
                for row in rows
            ],
        }
        manifest_data = json.dumps(bundle_manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.store.put_bytes(
            self.store.name(f"manifests/bundles/{archive_sha}.json"), manifest_data, overwrite=False
        )
        for row in rows:
            self.catalog.mark_content_uploaded(
                row["sha256"], object_name, f"blobs/{row['sha256']}",
                archive_sha, tar_path.stat().st_size, etag,
            )
        tar_path.unlink()

    def publish_snapshot(self, snapshot_id: str) -> str:
        entries = []
        missing = []
        for row in self.catalog.snapshot_manifest_entries(snapshot_id):
            item = {key: row[key] for key in row.keys() if row[key] is not None}
            if row["entry_type"] == "file" and not row["object_name"]:
                missing.append(row["relative_path"])
            entries.append(item)
        if missing:
            raise BackupIncomplete(f"Hay {len(missing)} archivos sin objeto remoto")
        document = {
            "schema": 1,
            "snapshot": snapshot_id,
            "created_at": utc_now(),
            "entries": entries,
        }
        payload = gzip.compress(
            json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            compresslevel=6,
        )
        digest = hashlib.sha256(payload).hexdigest()
        object_name = self.store.name(f"manifests/snapshots/{snapshot_id}-{digest}.json.gz")
        self.store.put_bytes(object_name, payload, overwrite=False)
        error_count = sum(item["entry_type"] == "error" for item in entries)
        self.catalog.set_snapshot_state(snapshot_id, "incomplete" if error_count else "complete",
                                        manifest=object_name, errors=error_count)
        return object_name

    def delete_verified_partial(self) -> None:
        if not self.settings.partial_path.exists():
            return
        root = self.settings.partial_path.resolve(strict=True)
        music = (Path.home() / "Music").resolve(strict=True)
        if root.parent != music or not root.name.startswith("file_scanner_usb_"):
            raise RuntimeError(f"Se rechazó eliminar una ruta inesperada: {root}")
        shutil.rmtree(root)

    def backup(self) -> dict:
        partial_manifest = self.catalog.get_meta("partial_manifest")
        if self.catalog.get_meta("partial_cleanup_pending", False):
            self.delete_verified_partial()
            self.catalog.set_meta("partial_cleanup_pending", False)
        if not self.catalog.get_meta("partial_uploaded", False):
            partial_id = self.import_partial()
            self.upload_pending(partial_id)
            partial_manifest = self.publish_snapshot(partial_id)
            self.catalog.set_meta("partial_manifest", partial_manifest)
            # Persistir primero permite retomar una interrupción durante el borrado.
            self.catalog.set_meta("partial_uploaded", True)
            self.catalog.set_meta("partial_cleanup_pending", True)
            self.delete_verified_partial()
            self.catalog.set_meta("partial_cleanup_pending", False)

        roots = find_device_roots(
            self.settings.device_friendly_name,
            serial_number=self.settings.device_serial_number,
            diagnostic_path=self.settings.runtime_path / "device-detection.jsonl",
        )
        if not roots:
            raise DeviceNotFound(self.settings.device_friendly_name)
        # El TOSHIBA esperado tiene una partición; si aparecen varias se separan por letra.
        errors = 0
        manifests = []
        for index, root in enumerate(roots, 1):
            snapshot_id = "toshiba-current" if len(roots) == 1 else f"toshiba-current-{index}"
            errors += self.scan_directory(root, snapshot_id)
            self.upload_pending(snapshot_id)
            manifests.append(self.publish_snapshot(snapshot_id))
        latest = {
            "schema": 1,
            "updated_at": utc_now(),
            "device": self.settings.device_friendly_name,
            "current_manifests": manifests,
            "historical_manifests": [partial_manifest] if partial_manifest else [],
            "errors": errors,
        }
        if errors:
            raise BackupIncomplete(f"El inventario terminó con {errors} errores; revisar el catálogo antes de confirmar el respaldo")
        self.store.put_bytes(
            self.store.name("manifests/latest.json"),
            json.dumps(latest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        return latest
