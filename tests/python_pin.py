"""The repository's pinned Python, enforced for every pytest session."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def pinned_python() -> tuple[int, int]:
    major, minor = (ROOT / ".python-version").read_text(encoding="utf-8").strip().split(".")[:2]
    return int(major), int(minor)


def require_pinned_python(version: tuple[int, ...] = tuple(sys.version_info)) -> None:
    # Runs at session start, before path, marker or shard selection, so every
    # CI pytest job (test shards, post-cutover, proof oracle, spanner
    # conformance) fails if it ends up on another interpreter.
    pinned = pinned_python()
    if tuple(version[:2]) != pinned:
        raise pytest.UsageError(
            f"Python {version[0]}.{version[1]} is running, but .python-version pins "
            f"{pinned[0]}.{pinned[1]} (production runs {pinned[0]}.{pinned[1]})."
        )
