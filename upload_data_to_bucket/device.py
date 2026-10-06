from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

POWERSHELL = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")

# Join both Windows providers by physical disk number, not volume labels.
INVENTORY_SCRIPT = r"""
$ErrorActionPreference='Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$errors = @()
$storage = @(); $wmi = @(); $parts = @()
$storageOK=$false; $wmiOK=$false; $partsOK=$false
try { $storage=@(Get-Disk -ErrorAction Stop); $storageOK=$true }
catch { $errors += 'Get-Disk: ' + $_.Exception.Message }
try { $wmi=@(Get-CimInstance Win32_DiskDrive -ErrorAction Stop); $wmiOK=$true }
catch { $errors += 'Win32_DiskDrive: ' + $_.Exception.Message }
try { $parts=@(Get-Partition -ErrorAction Stop); $partsOK=$true }
catch { $errors += 'Get-Partition: ' + $_.Exception.Message }
$numbers=@(@($storage | ForEach-Object { [int]$_.Number }) + @($wmi | ForEach-Object { [int]$_.Index }) | Sort-Object -Unique)
$records=@(foreach($number in $numbers) {
    $sd=$storage | Where-Object Number -eq $number | Select-Object -First 1
    $wd=$wmi | Where-Object Index -eq $number | Select-Object -First 1
    $mounts=@(); $boot=[bool]$sd.IsBoot; $system=[bool]$sd.IsSystem
    $mapped=$false
    if($sd -and $partsOK) {
        $mapped=$true
        foreach($p in @($parts | Where-Object DiskNumber -eq $number)) {
            $boot=$boot -or [bool]$p.IsBoot; $system=$system -or [bool]$p.IsSystem
            if($p.DriveLetter) { $mounts += ([string]$p.DriveLetter + ':\') }
            else {
                $paths=@($p.AccessPaths | Where-Object { $_ })
                $folder=@($paths | Where-Object { $_ -match '^[A-Za-z]:\\' })
                if($folder.Count) { $mounts += $folder[0] }
                elseif($paths.Count) { $mounts += $paths[0] }
            }
        }
    }
    if($wd) {
        try {
            $wp=@(Get-CimAssociatedInstance -InputObject $wd -Association Win32_DiskDriveToDiskPartition -ErrorAction Stop)
            foreach($p in $wp) {
                # Win32 BootPartition means active partition, not necessarily Windows.
                $logical=@(Get-CimAssociatedInstance -InputObject $p -Association Win32_LogicalDiskToPartition -ErrorAction Stop)
                foreach($l in $logical) { $mounts += ([string]$l.DeviceID + '\') }
            }
            $mapped=$true
        } catch { $errors += ('Disk ' + $number + ' associations: ' + $_.Exception.Message) }
    }
    $mounts=@($mounts | Sort-Object -Unique)
    if($mounts -contains ($env:SystemDrive + '\')) { $system=$true }
    [pscustomobject]@{
        DiskNumber=$number; FriendlyName=[string]$sd.FriendlyName; Model=[string]$wd.Model
        SerialNumbers=@([string]$sd.SerialNumber,[string]$wd.SerialNumber)
        PnpDeviceId=[string]$wd.PNPDeviceID; BusType=[string]$sd.BusType
        IsBoot=$boot; IsSystem=$system; IsOffline=[bool]$sd.IsOffline
        MappingSucceeded=$mapped; Mounts=$mounts
    }
})
[pscustomobject]@{InventorySucceeded=($storageOK -or $wmiOK); Disks=$records; Errors=$errors} | ConvertTo-Json -Depth 6 -Compress
"""


def _serial(value: str) -> str:
    return str(value or "").replace("\x00", "").strip().casefold()


def _model(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value or "").strip()).casefold()
    return re.sub(r"\s+(?:usb|scsi)\s+(?:disk\s+)?device$", "", value)


def select_device_roots(records: list[dict], friendly_name: str, serial_number: str = "") -> tuple[list[Path], list[dict]]:
    expected_serial = _serial(serial_number)
    matches = []
    decisions = []
    for record in records:
        names = {_model(record.get(k, "")) for k in ("FriendlyName", "Model")}
        serials = {_serial(s) for s in record.get("SerialNumbers", [])}
        decision = {"disk": record, "reason": "identity_mismatch"}
        decisions.append(decision)
        # A configured serial must match; a same-model foreign disk is rejected.
        identity = expected_serial in serials if expected_serial else bool(_model(friendly_name)) and _model(friendly_name) in names
        if not identity:
            continue
        if record.get("IsBoot") or record.get("IsSystem"):
            decision["reason"] = "system_disk_rejected"
            continue
        matches.append((record, decision))
    if len(matches) > 1:
        raise RuntimeError("Varios discos coinciden con la identidad configurada; no se selecciona ninguno")
    roots = []
    for record, decision in matches:
        if record.get("IsOffline"):
            decision["reason"] = "disk_offline"
            continue
        if not record.get("MappingSucceeded"):
            raise RuntimeError("No se pudieron consultar las particiones del disco identificado")
        decision["reason"] = "no_accessible_mount"
        decision["mount_checks"] = []
        for mount in record.get("Mounts", []):
            if not (re.match(r"^[A-Za-z]:\\", mount) or mount.startswith("\\\\?\\Volume{")):
                continue
            root = Path(mount)
            try:
                accessible = root.is_dir()
                decision["mount_checks"].append({"path": mount, "accessible": accessible})
            except OSError as error:
                decision["mount_checks"].append({"path": mount, "error": str(error)})
                continue
            if accessible:
                roots.append(root)
                decision["reason"] = "selected"
        if roots and any(not check.get("accessible") for check in decision["mount_checks"]):
            raise RuntimeError("El disco tiene volúmenes inaccesibles; no se confirma un respaldo parcial como completo")
    return sorted(set(roots), key=str), decisions


def find_device_roots(friendly_name: str, *, serial_number: str = "", diagnostic_path: Path | None = None) -> list[Path]:
    report = {"timestamp": datetime.now(timezone.utc).isoformat(), "expected_name": friendly_name,
              "expected_serial": serial_number, "status": "error"}
    try:
        completed = subprocess.run(
            [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", INVENTORY_SCRIPT],
            check=True, capture_output=True, text=True, encoding="utf-8", timeout=90,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        inventory = json.loads(completed.stdout)
        report["inventory"] = inventory
        if not inventory.get("InventorySucceeded"):
            raise RuntimeError("Windows no pudo enumerar los discos: " + "; ".join(inventory.get("Errors", [])))
        roots, decisions = select_device_roots(inventory.get("Disks", []), friendly_name, serial_number)
        report.update(status="found" if roots else "not_found", roots=[str(p) for p in roots], decisions=decisions)
        return roots
    except Exception as error:
        report["error"] = str(error)
        if isinstance(error, subprocess.CalledProcessError):
            report["stderr"] = (error.stderr or "")[-4000:]
        raise
    finally:
        if diagnostic_path is not None:
            diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
            with diagnostic_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(report, ensure_ascii=False) + "\n")
