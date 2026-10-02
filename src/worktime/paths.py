"""Stable per-user deployment paths; explicit roots keep tests isolated."""
from collections.abc import Mapping
import os
from pathlib import Path


def database_path(*, environ: Mapping[str, str] | None = None,
                  base_directory: str | Path | None = None) -> Path:
    if base_directory is not None:
        root = Path(base_directory).resolve()
    else:
        values = os.environ if environ is None else environ
        value = values.get("LOCALAPPDATA", "")
        if not value.strip() or not Path(value).is_absolute():
            raise ValueError("LOCALAPPDATA muss einen absoluten Benutzerpfad enthalten.")
        root = Path(value) / "WorkTime"
    path = root / "data" / "worktime.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
