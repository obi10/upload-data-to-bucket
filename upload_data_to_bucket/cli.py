from __future__ import annotations

import argparse
import json
import sys
import traceback
import tempfile
from pathlib import Path

from .backup import BackupEngine, BackupIncomplete, DeviceNotFound
from .catalog import Catalog
from .config import Settings, load_settings, write_default
from .lab import run_lab
from .notify import notify
from .device import find_device_roots
from .source_io import backup_access, open_source
from .restore import Restorer, RestorePaused
from .store import OciStore


def components(settings: Settings):
    settings.runtime_path.mkdir(parents=True, exist_ok=True)
    catalog = Catalog(settings.runtime_path / "catalog.sqlite3")
    store = OciStore(settings, catalog)
    return catalog, store


def command_backup(settings: Settings) -> int:
    catalog, store = components(settings)
    if catalog.get_meta("backup_complete", False):
        print(json.dumps({"estado": "ya_completo", "detalle": catalog.get_meta("last_result")}, ensure_ascii=False, indent=2))
        return 0
    attempts = int(catalog.get_meta("failure_attempts", 0))
    try:
        result = BackupEngine(settings, catalog, store).backup()
        catalog.set_meta("failure_attempts", 0)
        catalog.set_meta("last_result", {"status": "complete", "result": result})
        catalog.set_meta("backup_complete", True)
        notify(
            settings.tailscale_target,
            "La copia completa del TOSHIBA terminó y fue verificada en OCI.",
            "OCI_RESPALDO_TOSHIBA_COMPLETO.txt",
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except DeviceNotFound as error:
        attempts += 1
        catalog.set_meta("failure_attempts", attempts)
        catalog.set_meta("last_result", {"status": "device_not_found", "error": str(error)})
        if attempts == 12:
            notify(settings.tailscale_target, f"El respaldo OCI agotó 12 intentos: {error}", "OCI_RESPALDO_TOSHIBA_FALLO.txt")
        print(json.dumps({"estado": "sin_dispositivo", "intento": attempts, "error": str(error)}, ensure_ascii=False))
        return 3
    except Exception as error:
        attempts += 1
        catalog.set_meta("failure_attempts", attempts)
        details = {"status": "error", "attempt": attempts, "error": str(error), "traceback": traceback.format_exc()}
        catalog.set_meta("last_result", details)
        if attempts == 12:
            notify(settings.tailscale_target, f"El respaldo OCI agotó 12 intentos: {error}", "OCI_RESPALDO_TOSHIBA_FALLO.txt")
        print(json.dumps(details, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


def command_verify(settings: Settings) -> int:
    catalog, store = components(settings)
    failures = []
    objects = {}
    for row in catalog.uploaded_contents():
        objects[row["object_name"]] = (row["object_sha256"], row["object_size"])
    for name, (digest, size) in objects.items():
        if not store.verified(name, digest, size):
            failures.append(name)
    result = {"objetos_verificados": len(objects) - len(failures), "fallos": failures}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not failures else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Respaldo deduplicado del TOSHIBA a OCI")
    parser.add_argument("--config", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("backup")
    sub.add_parser("verify")
    sub.add_parser("diagnose-device")
    sub.add_parser("diagnose-access")
    restore = sub.add_parser("restore")
    restore.add_argument("--destination", type=Path, required=True)
    restore.add_argument("--manifest", action="append")
    restore.add_argument("--include-historical", action="store_true")
    existing = restore.add_mutually_exclusive_group()
    existing.add_argument("--overwrite", action="store_true")
    existing.add_argument("--resume", action="store_true",
                          help="Verifica y omite archivos ya completos; no los reemplaza")
    restore.add_argument("--progress-file", type=Path,
                         help="Bitácora JSONL de objetos y archivos verificados/restaurados")
    restore.add_argument("--pause-file", type=Path,
                         help="Pausa de forma segura cuando aparece este archivo")
    sub.add_parser("lab-test")
    init = sub.add_parser("init-config")
    init.add_argument("--output", type=Path, required=True)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init-config":
        write_default(args.output)
        return 0
    settings = load_settings(args.config)
    if args.command == "diagnose-access":
        # Tests the actual execution token with generated data only; no OCI/disks.
        settings.runtime_path.mkdir(parents=True, exist_ok=True)
        report = {"status": "error"}
        try:
            with tempfile.TemporaryDirectory(prefix="access-check-", dir=settings.runtime_path) as temp:
                path = Path(temp) / "sample.txt"
                path.write_bytes(b"backup access check")
                with backup_access([Path(temp)], enabled=True):
                    with open_source(path) as stream:
                        if stream.read() != b"backup access check":
                            raise RuntimeError("La lectura de laboratorio no coincide")
            report = {"status": "ok", "SeBackupPrivilege": "enabled_and_restored"}
        except Exception as error:
            report["error"] = str(error)
        (settings.runtime_path / "backup-access-check.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["status"] == "ok" else 2
    if args.command == "diagnose-device":
        diagnostic_path = settings.runtime_path / "device-detection.jsonl"
        roots = find_device_roots(settings.device_friendly_name,
                                  serial_number=settings.device_serial_number,
                                  diagnostic_path=diagnostic_path)
        print(json.dumps({"roots": [str(p) for p in roots], "diagnostics": str(diagnostic_path)}, ensure_ascii=False))
        return 0 if roots else 3
    if args.command == "backup":
        return command_backup(settings)
    if args.command == "verify":
        return command_verify(settings)
    if args.command == "lab-test":
        print(json.dumps(run_lab(settings), ensure_ascii=False, indent=2))
        return 0
    if args.command == "restore":
        _, store = components(settings)
        restorer = Restorer(settings, store)
        manifests = args.manifest or restorer.latest_manifest_names(args.include_historical)
        progress_file = args.progress_file
        if args.resume and progress_file is None:
            progress_file = settings.runtime_path / "restore-progress.jsonl"
        try:
            result = restorer.restore(
                args.destination, manifests, overwrite=args.overwrite,
                resume=args.resume, progress_file=progress_file, pause_file=args.pause_file,
            )
        except RestorePaused as error:
            print(json.dumps({"estado": "pausada", "detalle": str(error),
                              "registro_progreso": str(progress_file)}, ensure_ascii=False))
            return 4
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    return 2
