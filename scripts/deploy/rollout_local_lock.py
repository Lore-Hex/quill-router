#!/usr/bin/env python3
"""Run one local rollout command under a fail-fast advisory operation lock."""

from __future__ import annotations

import fcntl
import os
import stat
import subprocess
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 4 or sys.argv[2] != "--":
        raise SystemExit("usage: rollout_local_lock.py LOCK -- COMMAND [ARG ...]")
    path = Path(sys.argv[1])
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SystemExit("rollout operation lock is not a regular file")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SystemExit("another local rollout operation holds the lock") from error
        return subprocess.run(sys.argv[3:], check=False).returncode  # noqa: S603
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
