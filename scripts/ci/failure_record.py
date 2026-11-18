"""Record which tests a CI shard failed: the shadow selection's evidence.

PYTEST_DONT_REWRITE: tests/conftest.py names this module in pytest_plugins,
and a test that runs pytest in-process names it again once it is imported;
without the marker pytest warns that it cannot rewrite it, and that warning
changes the inner session's summary line (tests/test_lock_order_guard.py).

tests/conftest.py loads it in GitHub Actions only (GITHUB_ACTIONS and
RUNNER_TEMP set), so it runs in every CI pytest, the deploy-gating test
shards among them. It must never change a session's outcome: every hook
catches everything, prints at most one line to stderr, and returns; a record
it cannot write is a record the comparison reports as unavailable.

Who writes: the session's controller only, $RUNNER_TEMP/test-failures/
failures.json. The controller is the process that creates that file (an
exclusive create when its session starts) and sets TR_FAILURE_RECORD_OWNER
for the processes it starts. Not an xdist worker, not a pytest a test runs
in-process (the module's record is taken), not a process a test starts:
with the owner variable it sees another process's; without it (a test that
starts pytest with a filtered environment) it finds the file already there.

What: every failed test's node ID (exact: spaces, parameters and xdist group
suffixes kept), every collector that failed (a module, or a directory whose
conftest.py failed), pytest's exit status, and whether the shard ran its
tests: "complete" is true only when pytest exited 0 or 1 and every test it
collected reported an outcome. An interrupted session, a crash, an xdist
worker past --max-worker-restart, or a session that never reached its end
leaves "complete": false, and the comparison says "unavailable", never 0.

Node IDs come from the test reports, not from junit XML: junit writes a
dotted classname and a test name, which do not round-trip to the node ID
when a directory or parameter holds a dot.
"""

from __future__ import annotations

import json
import os
import sys
import weakref
from typing import Any

OWNER_ENV = "TR_FAILURE_RECORD_OWNER"
DIRECTORY = "test-failures"
NAME = "failures.json"


class Record:
    """Registered on the owning session's plugin manager only, so a pytest
    a test runs in-process reports to its own session, not to this one."""

    def __init__(self, config: Any, path: str) -> None:
        self.config_ref = weakref.ref(config)
        self.path = path
        self.failed: set[str] = set()
        self.collection: set[str] = set()
        self.reported: set[str] = set()   # tests whose outcome is known
        self.group = _option(config, "group")
        self.splits = _option(config, "splits")
        self.broken = False

    def write(self, *, complete: bool, exitstatus: int | None = None, collected: int | None = None) -> None:
        if self.broken:
            return
        try:
            body = {"group": self.group, "splits": self.splits, "complete": complete, "exitstatus": exitstatus,
                    "collected": collected, "reported": len(self.reported), "pid": os.getpid(),
                    "failed": sorted(self.failed), "collection_errors": sorted(self.collection)}
            temporary = self.path + ".tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(body, handle)
            os.replace(temporary, self.path)
        except Exception as error:
            self.give_up("write", error)

    def give_up(self, where: str, error: BaseException) -> None:
        if not self.broken:
            self.broken = True
            _say(f"{where}: {type(error).__name__}: {error}")

    def pytest_runtest_logreport(self, report: Any) -> None:
        try:
            if report.when == "call" or not report.passed:
                self.reported.add(report.nodeid)
            if report.failed:
                self.failed.add(report.nodeid)
        except Exception as error:
            self.give_up("report", error)

    def pytest_collectreport(self, report: Any) -> None:
        try:
            if report.failed:
                self.collection.add(report.nodeid)
        except Exception as error:
            self.give_up("collection report", error)

    def pytest_sessionfinish(self, session: Any, exitstatus: Any) -> None:
        try:
            if self.config_ref() is not session.config:
                return
            status = int(exitstatus)
            collected = int(getattr(session, "testscollected", -1))
            ran_everything = status in (0, 1) and len(self.reported) == collected
            self.write(complete=ran_everything, exitstatus=status, collected=collected)
        except Exception as error:
            self.give_up("session end", error)


def _say(message: str) -> None:
    try:
        print(f"failure_record: {message} (the shadow comparison will say it is unavailable)", file=sys.stderr)
    except Exception:  # noqa: S110 - nothing left to tell
        pass


def _option(config: Any, name: str) -> int | None:
    try:
        value = config.getoption(name)
    except Exception:
        return None
    return value if isinstance(value, int) else None


def _claim(path: str) -> bool:
    """Create the record exclusively: True for the session's controller."""
    owner = os.environ.get(OWNER_ENV)
    if owner is not None and owner != str(os.getpid()):
        return False  # an xdist worker, or a process the controller's tests started
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
    except FileExistsError:
        return False  # another session's record: a test started pytest with a filtered environment
    os.environ[OWNER_ENV] = str(os.getpid())
    return True


RECORD: Record | None = None


def pytest_configure(config: Any) -> None:
    global RECORD
    try:
        if RECORD is not None or hasattr(config, "workerinput"):
            return  # a pytest a test runs in-process, or an xdist worker
        temp = os.environ.get("RUNNER_TEMP")
        if not temp:
            return
        path = os.path.join(temp, DIRECTORY, NAME)
        if not _claim(path):
            return
        RECORD = Record(config, path)
        config.pluginmanager.register(RECORD, "failure-record")
        RECORD.write(complete=False)
    except Exception as error:
        _say(f"setup: {type(error).__name__}: {error}")
