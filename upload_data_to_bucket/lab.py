from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import uuid
import zipfile
from dataclasses import replace
from pathlib import Path

from .backup import BackupEngine
from .catalog import Catalog
from .config import Settings
from .restore import Restorer
from .store import OciStore, sha256_file


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in root.rglob("*") if path.is_file() and ".oci-restore-cache" not in path.parts
    }


def run_lab(settings: Settings) -> dict:
    run_id = uuid.uuid4().hex[:12]
    base = Path(tempfile.mkdtemp(prefix="oci_backup_lab_"))
    source = base / "source"
    restored = base / "restored"
    runtime = base / "runtime"
    source.mkdir()
    (source / "empty-dir").mkdir()
    (source / "nested" / "más datos").mkdir(parents=True)
    (source / "empty.bin").write_bytes(b"")
    duplicate = b"contenido duplicado\n" * 2000
    (source / "duplicate-a.txt").write_bytes(duplicate)
    (source / "nested" / "duplicate-b.txt").write_bytes(duplicate)
    (source / "nested" / "más datos" / "archivo-ñ.txt").write_text("áéíóú", encoding="utf-8")
    (source / "random.bin").write_bytes(os.urandom(1024 * 1024))
    with zipfile.ZipFile(source / "compressed.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("inside.txt", "archivo comprimido")
    # 23 MiB fuerza tres partes con el tamaño de laboratorio de 11 MiB.
    large = source / "large-lab.bin"
    block = hashlib.sha256(b"laboratorio OCI").digest() * 32768
    with large.open("wb") as output:
        for _ in range(23):
            output.write(block)

    lab_settings = replace(
        settings,
        prefix=f"{settings.prefix}/lab/{run_id}",
        runtime_dir=str(runtime),
        bundle_size=2 * 1024**2,
        large_file_threshold=2 * 1024**2,
        part_size=11 * 1024**2,
        parallel_parts=2,
        reserve_free_space=16 * 1024**2,
    )
    catalog = Catalog(runtime / "catalog.sqlite3")
    store = OciStore(lab_settings, catalog)
    try:
        engine = BackupEngine(lab_settings, catalog, store)
        snapshot_id = f"lab-{run_id}"
        errors = engine.scan_directory(source, snapshot_id, kind="lab")
        if errors:
            raise RuntimeError(f"El laboratorio generó {errors} errores de inventario")
        # Forzar una interrupción real después de crear el multipart upload.
        original_upload_part = store.client.upload_part
        interruption = {"done": False}
        interruption_lock = threading.Lock()

        def fail_one_part(*args, **kwargs):
            with interruption_lock:
                if not interruption["done"]:
                    interruption["done"] = True
                    raise ConnectionError("interrupción de laboratorio")
            return original_upload_part(*args, **kwargs)

        store.client.upload_part = fail_one_part
        try:
            engine.upload_pending()
            raise RuntimeError("La interrupción multipartes de laboratorio no ocurrió")
        except ConnectionError:
            pass
        finally:
            store.client.upload_part = original_upload_part
        if not interruption["done"]:
            raise RuntimeError("No se ejercitó la reanudación multipartes")
        # Reutiliza upload_id y conserva las partes que OCI ya recibió.
        engine.upload_pending()
        manifest = engine.publish_snapshot(snapshot_id)
        result = Restorer(lab_settings, store).restore(restored, [manifest])
        expected = tree_hashes(source)
        actual = tree_hashes(restored)
        if expected != actual:
            raise RuntimeError("La restauración del laboratorio no coincide con el origen")
        return {
            "estado": "completo",
            "run_id": run_id,
            "manifest": manifest,
            "archivos": len(expected),
            "reanudacion_multipartes": True,
            "restauracion": result,
        }
    finally:
        try:
            deleted = store.delete_prefix(f"{lab_settings.prefix}/")
            store.abort_multipart_prefix(f"{lab_settings.prefix}/")
        except Exception:
            deleted = -1
        catalog.close()
        shutil.rmtree(base, ignore_errors=True)
