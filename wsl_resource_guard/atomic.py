"""The one durable replace-by-rename used for state, ledgers and configuration.

Readers never see a partial file: content goes to a temporary file in the same
directory, is flushed to disk, and only then renamed over the target. The
directory entry is synced as well where the filesystem supports it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile


def write_text(path: Path, text: str, *, mode: int = 0o600,
               owner: tuple[int, int] | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            os.fchmod(handle.fileno(), mode)
            if owner is not None:
                os.fchown(handle.fileno(), *owner)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    _sync_directory(path.parent)


def write_json(path: Path, value: object, *, mode: int = 0o600,
               owner: tuple[int, int] | None = None, indent: int | None = None,
               compact: bool = False, trailing_newline: bool = False) -> None:
    """Serialize first, so a value that cannot be encoded never creates a file."""
    text = json.dumps(value, ensure_ascii=False, indent=indent,
                      separators=(',', ':') if compact else None)
    write_text(path, text + ('\n' if trailing_newline else ''), mode=mode, owner=owner)


def _sync_directory(directory: Path) -> None:
    # The rename is already visible; syncing the entry only narrows the window
    # in which a crash could lose it. Some filesystems refuse this call.
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
