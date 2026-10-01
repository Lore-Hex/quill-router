"""Shared reader for the DDL a recorded migration script submitted."""

from __future__ import annotations

from .deploy_script_harness import HarnessRun


def recorded_ddls(run: HarnessRun) -> list[str]:
    """Return every ``--ddl=`` argument of the run's ``gcloud spanner databases ddl update`` calls."""
    return [
        argument.removeprefix("--ddl=").replace(r"\n", "\n")
        for call in run.calls
        if call[:5] == ["gcloud", "spanner", "databases", "ddl", "update"]
        for argument in call
        if argument.startswith("--ddl=")
    ]
