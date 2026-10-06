from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from .backup import BackupEngine, BackupIncomplete, DeviceNotFound
from .catalog import Catalog
from .config import Settings, load_settings, write_default
from .lab import run_lab
from .notify import notify
from .device import find_device_roots
from .restore import Restorer
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
    restore = sub.add_parser("restore")
    restore.add_argument("--destination", type=Path, required=True)
    restore.add_argument("--manifest", action="append")
    restore.add_argument("--include-historical", action="store_true")
    restore.add_argument("--overwrite", action="store_true")
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
        result = restorer.restore(args.destination, manifests, overwrite=args.overwrite)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    return 2
