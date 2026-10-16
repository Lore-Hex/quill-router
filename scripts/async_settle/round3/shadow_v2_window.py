"""Compatibility name for the public source-derived window module."""
import sys

from scripts.async_settle import shadow_v2_window as implementation

sys.modules[__name__] = implementation

if __name__ == '__main__':
    raise SystemExit(implementation.main())
