from __future__ import annotations

import json
import subprocess
from pathlib import Path


POWERSHELL = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")


def find_device_roots(friendly_name: str) -> list[Path]:
    script = (
        "$items=@(Get-Partition -ErrorAction Stop | Where-Object DriveLetter | ForEach-Object {"
        "$d=Get-Disk -Number $_.DiskNumber -ErrorAction Stop;"
        "[pscustomobject]@{DriveLetter=[string]$_.DriveLetter;FriendlyName=[string]$d.FriendlyName;"
        "BusType=[string]$d.BusType;IsBoot=[bool]$d.IsBoot;IsSystem=[bool]$d.IsSystem}});"
        "$items|ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
        check=True, capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if not completed.stdout.strip():
        return []
    records = json.loads(completed.stdout)
    if isinstance(records, dict):
        records = [records]
    expected = friendly_name.casefold().strip()
    roots = []
    for record in records:
        letter = str(record.get("DriveLetter", "")).strip()
        name = str(record.get("FriendlyName", "")).casefold().strip()
        if len(letter) == 1 and name == expected and not record.get("IsBoot") and not record.get("IsSystem"):
            root = Path(f"{letter.upper()}:\\")
            if root.exists():
                roots.append(root)
    return sorted(set(roots), key=str)
