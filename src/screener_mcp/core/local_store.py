"""
Safe JSON storage for the user's own data (portfolio, research notes).

This data exists nowhere else, so two rules apply:

  * A write never leaves a half-written file. It goes to a temp file in the same
    folder, is flushed to disk, and then atomically replaces the original. The
    previous version is kept next to it as ``<name>.bak``.
  * A file that exists but can't be read is an error, never "empty". Treating it
    as empty meant the next save overwrote every holding or note with nothing.
"""

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .envelope import ToolError

logger = logging.getLogger(__name__)


def backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def read_json(path: Path, default: Any) -> Any:
    """Parsed contents of ``path``, or ``default`` if it doesn't exist yet.

    Raises ToolError("local_data_unreadable") if the file exists but isn't valid
    JSON, naming the backup so the user can restore it. Nothing is changed.
    """
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        bak = backup_path(path)
        hint = (
            f" The previous version is saved at {bak}: copy it over {path.name} to restore it."
            if bak.exists() else " No backup exists yet."
        )
        logger.error(f"Unreadable local data file {path}: {e}")
        raise ToolError(
            f"{path} could not be read ({type(e).__name__}), so nothing was changed.{hint}",
            "local_data_unreadable",
            path=str(path),
            backup=str(bak) if bak.exists() else None,
        ) from e


def write_json(path: Path, data: Any) -> None:
    """Atomically replace ``path`` with ``data``, keeping the old file as ``.bak``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            shutil.copy2(path, backup_path(path))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
