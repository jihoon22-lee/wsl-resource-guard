"""Bounded reads of regular files, including paths selected by a local owner.

O_NONBLOCK prevents opening a FIFO from waiting before fstat can reject it.
Permission checks are the caller's responsibility: privileged readers must first
switch to the owner, including for symlink targets and parent directories.
"""
import errno
import os
from pathlib import Path
import stat
import time


def iter_text_lines(path: Path, *, max_line_bytes: int = 1024 * 1024,
                    deadline: float):
    """Yield UTF-8 lines within the file size observed at open, without newlines.

    The byte bound includes the newline. Appends after fstat belong to the next
    read; truncation during this read is an error, not a successful empty day.
    Symlinks are allowed only within the caller's already-dropped credentials.
    """
    if max_line_bytes <= 0:
        raise ValueError('Line limit must be positive')
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EINVAL, 'Only regular files may be read')
        remaining = info.st_size
        pending = bytearray()
        while remaining:
            if time.monotonic() >= deadline:
                raise TimeoutError('History read timed out')
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                raise OSError(errno.EIO, 'File truncated during read')
            remaining -= len(chunk)
            pending.extend(chunk)
            start = 0
            while (end := pending.find(b'\n', start)) >= 0:
                if end + 1 - start > max_line_bytes:
                    raise OSError(errno.EFBIG, 'History line exceeds read limit')
                if time.monotonic() >= deadline:
                    raise TimeoutError('History read timed out')
                yield pending[start:end].decode('utf-8', errors='replace')
                start = end + 1
            del pending[:start]
            if len(pending) > max_line_bytes:
                raise OSError(errno.EFBIG, 'History line exceeds read limit')
        if time.monotonic() >= deadline:
            raise TimeoutError('History read timed out')
        if pending:
            yield pending.decode('utf-8', errors='replace')
    finally:
        os.close(fd)


def read_text(path: Path, *, max_bytes: int = 4 * 1024 * 1024,
              encoding: str = 'utf-8', errors: str = 'strict', tail: bool = False) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EINVAL, 'Only regular files may be read')
        if not tail and info.st_size > max_bytes:
            raise OSError(errno.EFBIG, 'File exceeds read limit')
        offset = max(0, info.st_size - max_bytes) if tail else 0
        os.lseek(fd, offset, os.SEEK_SET)
        data = bytearray()
        while len(data) <= max_bytes:
            chunk = os.read(fd, min(65536, max_bytes + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > max_bytes:
            raise OSError(errno.EFBIG, 'File exceeds read limit')
        if offset:
            _, separator, remainder = data.partition(b'\n')
            data = remainder if separator else b''
        return data.decode(encoding, errors=errors)
    finally:
        os.close(fd)
