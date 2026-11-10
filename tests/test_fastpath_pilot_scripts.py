"""The pilot's watch and hourly audit scripts, kept in the repository so a
node's replacement does not lose them (docs/runbooks/fastpath-deploy.md)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from tests.test_fastpath_lists import resolved_lists

PILOT = Path(__file__).resolve().parents[1] / "scripts" / "fastpath" / "pilot"


def test_both_scripts_parse_and_are_executable() -> None:
    for name in ("pilot-watch-ws.sh", "pilot-audit-hourly.sh"):
        path = PILOT / name
        assert path.stat().st_mode & 0o111, name
        subprocess.run(  # noqa: S603 - fixed executable on a repository file
            [shutil.which("bash") or "/bin/bash", "-n", str(path)], check=True
        )


def test_the_watch_keeps_its_approved_tolerances() -> None:
    """-misses 10 and -window 25m were approved after Monitoring's Internal
    bursts tripped the default (2026-10-29); -max-undelivered 1000 is the
    production ceiling, not a load run's."""
    watch = (PILOT / "pilot-watch-ws.sh").read_text(encoding="utf-8")
    for flag in ("-misses 10", "-window 25m", "-fresh 15m", "-max-undelivered 1000", "-max-cpu 0.35"):
        assert flag in watch, flag
    assert "audit $WS; rc=$?" in watch and "PILOT_WATCH_ABORTED" in watch


def test_the_hourly_audit_covers_the_include_list() -> None:
    # The explicit pilot set is empty since WS2 (all mode); the workspaces
    # the audit names are the enabler's include list, the set as it stood.
    lists = resolved_lists()
    assert lists["TR_FASTPATH_PILOT_WORKSPACES"] == ""
    workspaces = lists["TR_FASTPATH_ENABLER_INCLUDE"].split(",")
    audit = (PILOT / "pilot-audit-hourly.sh").read_text(encoding="utf-8")
    listed = next(line for line in audit.splitlines() if line.startswith("WORKSPACES="))
    assert sorted(listed.split("=", 1)[1].strip('"').split()) == sorted(workspaces)
