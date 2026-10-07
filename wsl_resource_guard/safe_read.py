"""Bounded reads of regular files, including paths selected by a local owner.

O_NONBLOCK prevents opening a FIFO from waiting before fstat can reject it.
Permission checks are the caller's responsibility: privileged readers must first
switch to the owner, including for symlink targets and parent directories.
"""
import errno
import os
from pathlib import Path
import stat


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
