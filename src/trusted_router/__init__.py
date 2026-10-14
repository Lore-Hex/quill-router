"""TrustedRouter."""

import os as _os

__all__ = ["__version__"]
__version__ = "0.1.0"

# Presence only: importing the package never takes ownership of an open file.
try:
    _os.fstat(3)
    LAUNCH_FD_PRESENT = True
except OSError:
    LAUNCH_FD_PRESENT = False
