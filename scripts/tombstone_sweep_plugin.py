"""pytest plugin for scripts/tombstone_sweep.py: record each failed test's exact node id.

Parsing pytest's summary lines truncates a parametrized id at its first space,
and a truncated id then matches nothing. The report object carries the id whole.
"""

from __future__ import annotations

import os
from typing import Any


def _record(line: str) -> None:
    path = os.environ.get("TOMBSTONE_SWEEP_FAILURES")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def pytest_runtest_logreport(report: Any) -> None:
    if report.failed:
        _record(report.nodeid)


def pytest_collectreport(report: Any) -> None:
    if report.failed:
        _record(report.nodeid or "(collection)")
