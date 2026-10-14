"""Serving bridge for an explicitly delivered launch descriptor."""
from __future__ import annotations

import os

from trusted_router import LAUNCH_FD_PRESENT

INHERITED_FD: int | None = None
_claimed = False


def claim() -> None:
    """Called by the serving entry point before importing serving libraries."""
    global INHERITED_FD, _claimed
    if _claimed:
        return
    _claimed = True
    if LAUNCH_FD_PRESENT:
        # Transfer fd 3 itself. No duplicate can outlive the serving owner.
        INHERITED_FD = 3


def close() -> None:
    """Release the transferred input after reading or at serving exit."""
    global INHERITED_FD
    fd, INHERITED_FD = INHERITED_FD, None
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


def take() -> int | None:
    """The reader borrows the owned input; close runs even on an invalid record."""
    return INHERITED_FD
