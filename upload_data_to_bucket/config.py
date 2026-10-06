from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    profile: str = "DEFAULT"
    region: str = "us-ashburn-1"
    namespace: str = ""
    bucket: str = ""
    bucket_ocid: str = ""
    prefix: str = "backups/toshiba"
    device_friendly_name: str = "TOSHIBA EXTERNAL_USB USB Device"
    device_serial_number: str = ""
    partial_root: str = ""
    runtime_dir: str = ""
    bundle_size: int = 4 * 1024**3
    large_file_threshold: int = 512 * 1024**2
    part_size: int = 128 * 1024**2
    parallel_parts: int = 4
    reserve_free_space: int = 5 * 1024**3
    tailscale_target: str = ""

    @property
    def runtime_path(self) -> Path:
        return Path(self.runtime_dir)

    @property
    def partial_path(self) -> Path:
        return Path(self.partial_root)

    def with_prefix(self, prefix: str) -> "Settings":
        return replace(self, prefix=prefix.strip("/"))


def load_settings(path: Path | None = None) -> Settings:
    if path is None:
        return Settings()
    values = json.loads(path.read_text(encoding="utf-8"))
    known = set(Settings.__dataclass_fields__)
    unexpected = sorted(set(values) - known)
    if unexpected:
        raise ValueError(f"Opciones desconocidas: {', '.join(unexpected)}")
    return Settings(**values)


def write_default(path: Path) -> None:
    path.write_text(
        json.dumps(asdict(Settings()), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
