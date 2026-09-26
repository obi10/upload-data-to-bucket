from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import tarfile
from collections import defaultdict
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

    def restore(self, destination: Path, manifest_names: list[str], overwrite=False) -> dict:
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
        for object_name, object_entries in grouped.items():
            first = object_entries[0]
            object_sha = first["object_sha256"]
            suffix = ".tar" if first.get("member_name") else ".blob"
            local_object = cache / f"{object_sha}{suffix}"
            if not local_object.exists() or sha256_file(local_object) != object_sha:
                local_object.unlink(missing_ok=True)
                self.store.download(object_name, local_object)
                if sha256_file(local_object) != object_sha:
                    raise RuntimeError(f"SHA-256 remoto inválido: {object_name}")
            archive = tarfile.open(local_object, "r") if suffix == ".tar" else None
            try:
                extracted = {}
                for entry in object_entries:
                    target = destination_path(destination, entry["relative_path"])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists() and not overwrite:
                        raise FileExistsError(target)
                    temporary = target.with_name(target.name + ".restoring")
                    temporary.unlink(missing_ok=True)
                    if archive:
                        digest = entry["sha256"]
                        source_path = extracted.get(digest)
                        if source_path:
                            shutil.copyfile(source_path, temporary)
                        else:
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
                    if archive:
                        extracted[entry["sha256"]] = target
                    if entry.get("mtime_ns"):
                        os.utime(target, ns=(entry["mtime_ns"], entry["mtime_ns"]))
                    restored += 1
            finally:
                if archive:
                    archive.close()
                local_object.unlink(missing_ok=True)
        cache.rmdir()
        return {"archivos_restaurados": restored, "errores_registrados": len(errors)}
