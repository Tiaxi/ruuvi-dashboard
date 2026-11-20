"""Crash-safe file writes."""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from pathlib import Path

_NEW_FILE_MODE = 0o644


def atomic_write_text(path: str | Path, text: str) -> None:
    """Replace a file's contents so readers see either the old or the new text.

    The text goes to a temporary file in the same directory, is flushed to
    disk, and then renamed over the destination. If anything fails, the
    previous file is left untouched. An existing file keeps its permission
    bits and, where the process is allowed to, its owner; a new file gets
    mode 0644 so other containers (such as Grafana) can read it. A symlinked
    destination stays a symlink; its target is replaced.
    """
    dest = Path(os.path.realpath(path))
    try:
        old = dest.stat()
    except FileNotFoundError:
        old = None

    fd, tmp_name = tempfile.mkstemp(
        dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            if old is None:
                os.fchmod(fd, _NEW_FILE_MODE)
            else:
                new = os.fstat(fd)
                if (old.st_uid, old.st_gid) != (new.st_uid, new.st_gid):
                    # Only root may give a file away; keep ours otherwise.
                    with contextlib.suppress(PermissionError):
                        os.fchown(fd, old.st_uid, old.st_gid)
                os.fchmod(fd, stat.S_IMODE(old.st_mode))
            tmp.write(text)
            tmp.flush()
            os.fsync(fd)
        os.replace(tmp_path, dest)  # noqa: PTH105 - tests patch os.replace
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise

    _fsync_dir(dest.parent)


def _fsync_dir(directory: Path) -> None:
    """Persist a rename by syncing its directory, where the OS supports it."""
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        with contextlib.suppress(OSError):
            os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
