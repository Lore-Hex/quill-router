"""Record which tests a CI shard failed: the shadow selection's evidence.

PYTEST_DONT_REWRITE: tests/conftest.py names this module in pytest_plugins,
and a test that runs pytest in-process names it again once it is imported;
without the marker pytest warns that it cannot rewrite it, and that warning
changes the inner session's summary line (tests/test_lock_order_guard.py).

tests/conftest.py loads it in GitHub Actions only (GITHUB_ACTIONS and
RUNNER_TEMP set). The session's controller (not an xdist worker, not a
process a test started, not a pytest a test runs in-process) writes
$RUNNER_TEMP/test-failures/failures.json twice: when the session starts,
with "complete": false, and when it ends, with every failed test's node ID
(exact: spaces, parameters and xdist group suffixes kept), every module that
failed to collect, and pytest's exit status. ci.yml's test job uploads the
file whatever the outcome, and the coverage job's `testmap.py compare` reads
every shard's: a shard whose file is missing or incomplete (cancelled, timed
out, crashed) makes the comparison unavailable, never a silent zero.

Node IDs come from the test reports, not from junit XML: junit writes a
dotted classname and a test name, which do not round-trip to the node ID
when a directory or parameter holds a dot.
"""

from __future__ import annotations

import json
import os
import weakref
from typing import Any

OWNER_ENV = "TR_FAILURE_RECORD_OWNER"
DIRECTORY = "test-failures"


def _owner() -> bool:
    """Is this process the session's controller? The first process to load
    the module owns the record (it sets OWNER_ENV); its xdist workers and any
    process a test starts see an owner that is not themselves."""
    owner = os.environ.get(OWNER_ENV)
    if owner is None:
        os.environ[OWNER_ENV] = str(os.getpid())
        return True
    return owner == str(os.getpid())


class Record:
    """Registered on the owning session's plugin manager only, so a pytest
    a test runs in-process reports to its own session, not to this one."""

    def __init__(self, config: Any) -> None:
        self.config_ref = weakref.ref(config)
        self.path = os.path.join(os.environ["RUNNER_TEMP"], DIRECTORY, "failures.json")
        self.failed: set[str] = set()
        self.collection: set[str] = set()
        self.group = _option(config, "group")
        self.splits = _option(config, "splits")

    def write(self, *, complete: bool, exitstatus: int | None = None) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        body = {"group": self.group, "splits": self.splits, "complete": complete, "exitstatus": exitstatus,
                "failed": sorted(self.failed), "collection_errors": sorted(self.collection)}
        temporary = self.path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(body, handle)
        os.replace(temporary, self.path)

    def pytest_runtest_logreport(self, report: Any) -> None:
        if report.failed:
            self.failed.add(report.nodeid)

    def pytest_collectreport(self, report: Any) -> None:
        if report.failed:
            self.collection.add(report.nodeid)

    def pytest_sessionfinish(self, session: Any, exitstatus: Any) -> None:
        if self.config_ref() is session.config:
            self.write(complete=True, exitstatus=int(exitstatus))


def _option(config: Any, name: str) -> int | None:
    try:
        value = config.getoption(name)
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, int) else None


RECORD: Record | None = None


def pytest_configure(config: Any) -> None:
    global RECORD
    if RECORD is not None or hasattr(config, "workerinput") or not os.environ.get("RUNNER_TEMP") or not _owner():
        return  # a nested session, a worker, or another process's
    RECORD = Record(config)
    config.pluginmanager.register(RECORD, "failure-record")
    RECORD.write(complete=False)
