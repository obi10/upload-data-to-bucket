from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


TAILSCALE = Path(r"C:\Program Files\Tailscale\tailscale.exe")


def notify(target: str, message: str, filename: str) -> bool:
    if not target or not TAILSCALE.exists():
        return False
    notice = Path(tempfile.gettempdir()) / filename
    notice.write_text(message.rstrip() + "\n", encoding="utf-8")
    try:
        subprocess.run(
            [str(TAILSCALE), "file", "cp", str(notice), f"{target}:"],
            check=True, capture_output=True, text=True, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        notice.unlink(missing_ok=True)
