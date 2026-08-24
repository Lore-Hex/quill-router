"""The cloud staleness detector.

The failure being guarded is a cloud that stops being deployed while staying
healthy. Azure ran that way for an unknown period on a deploy script that could
no longer address it: answering 200 the whole time, with nothing asking how old
the healthy thing was.

Every assertion here is about a plane that is UP. A detector that only fires
when something is down would not have caught it.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_cloud_staleness.py"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("check_cloud_staleness", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: the module defines a dataclass with
    # `from __future__ import annotations`, so its field types are strings that
    # dataclasses resolves by looking the module up in sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def _inspect(monkeypatch: pytest.MonkeyPatch, *, document: Any, committed: datetime | None) -> Any:
    staleness = _module()
    monkeypatch.setattr(
        staleness,
        "_fetch",
        lambda url: (
            document if not isinstance(document, Exception) else (_ for _ in ()).throw(document)
        ),
    )
    monkeypatch.setattr(staleness, "_commit_date", lambda sha: committed)
    return staleness.inspect("azure", "https://azure.example", NOW)


def test_a_fresh_plane_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _inspect(
        monkeypatch,
        document={"plane": "azure.trustedrouter.com", "release": "abc1234"},
        committed=NOW - timedelta(hours=5),
    )

    assert state.ok
    assert state.age_hours == pytest.approx(5.0, abs=0.1)


def test_a_plane_running_old_code_reports_its_age(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _inspect(
        monkeypatch,
        document={"release": "abc1234"},
        committed=NOW - timedelta(days=9),
    )

    assert state.ok  # readable, not a problem -- staleness is judged by the caller
    assert state.age_hours == pytest.approx(216.0, abs=0.1)


def test_a_constant_release_string_is_not_treated_as_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact pre-fix state. AWS shipped TR_RELEASE="eu" and Azure "azure",
    so every deploy reported an identical string. Parsing that as a version
    would have called a plane frozen for months perfectly current."""
    for constant in ("eu", "azure", "local", "unknown"):
        state = _inspect(monkeypatch, document={"release": constant}, committed=NOW)
        assert not state.ok, constant
        assert "not a commit" in state.problem


def test_a_commit_this_repo_does_not_have_is_a_finding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unresolvable is not fresh. It means the plane is running something built
    from a commit that is not in this history."""
    state = _inspect(monkeypatch, document={"release": "deadbee"}, committed=None)

    assert not state.ok
    assert "not in this repository" in state.problem


def test_a_plane_without_the_endpoint_is_not_silently_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 404 means the plane predates the version endpoint, which is itself
    evidence it has not been deployed recently. Skipping it would hide exactly
    the plane most likely to be stale."""
    import urllib.error

    staleness = _module()
    monkeypatch.setattr(
        staleness,
        "_fetch",
        lambda url: (_ for _ in ()).throw(
            urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]
        ),
    )
    state = staleness.inspect("aws", "https://aws.example", NOW)

    assert not state.ok
    assert "cannot be read" in state.problem


def test_an_unreachable_plane_is_a_problem_not_a_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staleness = _module()
    monkeypatch.setattr(
        staleness, "_fetch", lambda url: (_ for _ in ()).throw(OSError("connection refused"))
    )
    state = staleness.inspect("gcp", "https://gcp.example", NOW)

    assert not state.ok
    assert "unreachable" in state.problem


def test_strict_mode_fails_on_an_unreadable_plane(monkeypatch: pytest.MonkeyPatch) -> None:
    """--strict is what a scheduled run uses. "We stopped being able to tell"
    and "everything is fine" must not both be a green tick."""
    staleness = _module()
    monkeypatch.setattr(staleness, "PLANES", (("azure", "https://azure.example"),))
    monkeypatch.setattr(staleness, "_fetch", lambda url: (_ for _ in ()).throw(OSError("nope")))
    monkeypatch.setattr("sys.argv", ["check_cloud_staleness.py", "--strict"])

    assert staleness.main() == 1


def test_lenient_mode_reports_without_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    staleness = _module()
    monkeypatch.setattr(staleness, "PLANES", (("azure", "https://azure.example"),))
    monkeypatch.setattr(staleness, "_fetch", lambda url: (_ for _ in ()).throw(OSError("nope")))
    monkeypatch.setattr("sys.argv", ["check_cloud_staleness.py"])

    assert staleness.main() == 0
