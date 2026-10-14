"""Retain the inherited descriptor before the application imports its runtime."""
import errno
import os

# Package initialization runs before serving libraries can allocate descriptor 3.
# Retain a duplicate so subsequent descriptor reuse cannot change this input.
try:
    INHERITED_FD: int | None = os.dup(3)
except OSError as error:
    # An unavailable supplied input is recorded by the background startup task.
    INHERITED_FD = None if error.errno == errno.EBADF else -1
