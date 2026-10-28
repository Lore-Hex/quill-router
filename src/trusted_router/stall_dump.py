"""Shared nonblocking stack-dump descriptor; no watchdog or startup side effects."""
from __future__ import annotations

import logging
import os

# Preserve the existing router fallback log names and behavior.
logger = logging.getLogger("trusted_router.serve")


def stall_dump_fd(stderr_fd: int) -> int:
    """A descriptor on stderr whose writes fail rather than wait when it is full.

    Reopening the descriptor through /proc gives the same pipe or file a file
    description of its own, so it can be non-blocking (and appending) without
    changing how anything else writes to stderr. Where that is not possible,
    stderr itself, logged.
    """
    was_blocking = os.get_blocking(stderr_fd)
    try:
        fd = os.open(
            f"/proc/self/fd/{stderr_fd}",
            os.O_WRONLY | os.O_APPEND | os.O_NONBLOCK | os.O_CLOEXEC,
        )
    except OSError as exc:
        logger.warning("serve.stall_dump_blocking reason=%s", type(exc).__name__)
        return stderr_fd
    if os.get_blocking(stderr_fd) != was_blocking:
        # One shared description after all: put stderr back as it was.
        os.close(fd)
        os.set_blocking(stderr_fd, was_blocking)
        logger.warning("serve.stall_dump_blocking reason=shared_description")
        return stderr_fd
    return fd
