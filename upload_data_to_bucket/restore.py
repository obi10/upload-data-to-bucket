from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import tarfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .store import OciStore, sha256_file


def destination_path(root: Path, relative: str) -> Path:
    candidate = root.joinpath(*Path(relative).parts)
    resolved_parent = candidate.parent.resolve()
    root_resolved = root.resolve()
    if resolved_parent != root_resolved and root_resolved not in resolved_parent.parents:
        raise ValueError(f"Ruta insegura en manifiesto: {relative}")
    return candidate


class RestorePaused(Exception):
    """A pause was requested at a safe file boundary."""


class ProgressJournal:
    def __init__(self, path: Path | None):
        self.path = path
        self.stream = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.stream = path.open("a", encoding="utf-8", buffering=1)

    def emit(self, event: str, **details) -> None:
        if self.stream is not None:
            record = {"time_utc": datetime.now(timezone.utc).isoformat(), "event": event, **details}
            self.stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def close(self) -> None:
        if self.stream is not None:
            self.stream.close()


class Restorer:
    def __init__(self, settings: Settings, store: OciStore):
        self.settings = settings
        self.store = store

    def load_manifest(self, object_name: str) -> dict:
        payload = self.store.get_bytes(object_name)
        if object_name.endswith(".gz"):
            payload = gzip.decompress(payload)
        return json.loads(payload.decode("utf-8"))

    def latest_manifest_names(self, include_historical=False) -> list[str]:
        latest = json.loads(
            self.store.get_bytes(self.store.name("manifests/latest.json")).decode("utf-8")
        )
        names = list(latest.get("current_manifests", []))
        if include_historical:
            names.extend(latest.get("historical_manifests", []))
        return names

    def restore(
        self, destination: Path, manifest_names: list[str], overwrite=False,
        resume=False, progress_file: Path | None = None, pause_file: Path | None = None,
    ) -> dict:
        if resume and overwrite:
            raise ValueError("--resume y --overwrite no pueden usarse juntos")
        destination.mkdir(parents=True, exist_ok=True)
        cache = destination / ".oci-restore-cache"
        cache.mkdir(exist_ok=True)
        entries = []
        for name in manifest_names:
            entries.extend(self.load_manifest(name)["entries"])

        # Si hay varias instantáneas, la última entrada de una ruta gana.
        by_path = {entry["relative_path"]: entry for entry in entries}
        for entry in by_path.values():
            if entry["entry_type"] == "directory":
                destination_path(destination, entry["relative_path"]).mkdir(parents=True, exist_ok=True)

        grouped = defaultdict(list)
        errors = []
        for entry in by_path.values():
            if entry["entry_type"] == "file":
                grouped[entry["object_name"]].append(entry)
            elif entry["entry_type"] == "error":
                errors.append(entry)

        restored = 0
        skipped = 0
        downloaded = 0
        journal = ProgressJournal(progress_file)

        def check_pause() -> None:
            if pause_file is not None and pause_file.exists():
                raise RestorePaused(f"Pausa solicitada mediante {pause_file}")

        journal.emit("run_started", destination=str(destination), manifests=manifest_names,
                     expected_files=sum(map(len, grouped.values())), resume=resume)
        try:
            for object_name, object_entries in grouped.items():
                check_pause()
                first = object_entries[0]
                object_sha = first["object_sha256"]
                suffix = ".tar" if first.get("member_name") else ".blob"
                local_object = cache / f"{object_sha}{suffix}"
                pending = []
                extracted = {}
                for entry in object_entries:
                    check_pause()
                    target = destination_path(destination, entry["relative_path"])
                    if target.exists():
                        if resume:
                            if not target.is_file() or target.is_symlink() or sha256_file(target) != entry["sha256"]:
                                raise RuntimeError(f"Archivo existente distinto del manifiesto; no se reemplazó: {target}")
                            skipped += 1
                            extracted.setdefault(entry["sha256"], target)
                            journal.emit("file_verified_existing", path=entry["relative_path"],
                                         bytes=entry["size"], sha256=entry["sha256"], object=object_name)
                            continue
                        if not overwrite:
                            raise FileExistsError(target)
                    pending.append((entry, target))
                if not pending:
                    local_object.unlink(missing_ok=True)
                    journal.emit("object_skipped", object=object_name, reason="all_files_verified")
                    continue

                needs_object = any(entry["sha256"] not in extracted for entry, _ in pending)
                archive = None
                if needs_object:
                    if not local_object.exists() or sha256_file(local_object) != object_sha:
                        local_object.unlink(missing_ok=True)
                        journal.emit("object_download_started", object=object_name,
                                     expected_bytes=first.get("object_size"))
                        last_reported = 0

                        def on_chunk(downloaded_bytes: int) -> None:
                            nonlocal last_reported
                            if downloaded_bytes - last_reported >= 128 * 1024**2:
                                journal.emit("object_download_progress", object=object_name,
                                             bytes=downloaded_bytes,
                                             expected_bytes=first.get("object_size"))
                                last_reported = downloaded_bytes
                            check_pause()

                        self.store.download(object_name, local_object, on_chunk=on_chunk)
                        if sha256_file(local_object) != object_sha:
                            raise RuntimeError(f"SHA-256 remoto inválido: {object_name}")
                        downloaded += 1
                        journal.emit("object_downloaded", object=object_name,
                                     bytes=local_object.stat().st_size, sha256=object_sha)
                    else:
                        journal.emit("object_cache_reused", object=object_name,
                                     bytes=local_object.stat().st_size)
                    archive = tarfile.open(local_object, "r") if suffix == ".tar" else None
                try:
                    for entry, target in pending:
                        check_pause()
                        target.parent.mkdir(parents=True, exist_ok=True)
                        temporary = target.with_name(target.name + ".restoring")
                        temporary.unlink(missing_ok=True)
                        source_path = extracted.get(entry["sha256"])
                        if source_path:
                            shutil.copyfile(source_path, temporary)
                        elif archive:
                            member = archive.getmember(entry["member_name"])
                            source = archive.extractfile(member)
                            if source is None:
                                raise RuntimeError(f"Miembro ausente: {entry['member_name']}")
                            with source, temporary.open("wb") as output:
                                shutil.copyfileobj(source, output, 8 * 1024**2)
                        else:
                            shutil.copyfile(local_object, temporary)
                        if sha256_file(temporary) != entry["sha256"]:
                            temporary.unlink(missing_ok=True)
                            raise RuntimeError(f"Verificación fallida al restaurar {target}")
                        os.replace(temporary, target)
                        extracted.setdefault(entry["sha256"], target)
                        if entry.get("mtime_ns"):
                            os.utime(target, ns=(entry["mtime_ns"], entry["mtime_ns"]))
                        restored += 1
                        journal.emit("file_restored", path=entry["relative_path"],
                                     bytes=entry["size"], sha256=entry["sha256"], object=object_name)
                finally:
                    if archive:
                        archive.close()
                # Keep a valid object in cache if paused or interrupted mid-object.
                local_object.unlink(missing_ok=True)
                journal.emit("object_completed", object=object_name)
            cache.rmdir()
            result = {
                "archivos_restaurados": restored + skipped,
                "archivos_nuevos": restored,
                "archivos_ya_verificados": skipped,
                "objetos_descargados": downloaded,
                "errores_registrados": len(errors),
                "registro_progreso": str(progress_file) if progress_file else None,
            }
            journal.emit("run_completed", **result)
            return result
        except RestorePaused:
            journal.emit("run_paused", files_completed=restored + skipped,
                         files_new=restored, files_already_verified=skipped)
            raise
        except BaseException as error:
            journal.emit("run_interrupted", error=repr(error),
                         files_completed=restored + skipped, files_new=restored)
            raise
        finally:
            journal.close()
