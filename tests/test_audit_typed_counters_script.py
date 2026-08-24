from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from scripts import audit_typed_counters


@dataclass
class _Report:
    clean: bool
    label: str
    samples: dict[str, object] = field(default_factory=dict)

    def summary(self) -> str:
        return self.label


class _Settings:
    storage_backend = "spanner-bigtable"


def _report_func(report: _Report) -> audit_typed_counters.AuditFunc:
    def inner(_store: Any, *, max_samples: int = 20) -> _Report:
        assert max_samples == 100_000
        return report

    return inner


def test_audit_script_exits_zero_when_invariants_clean(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("TR_STORAGE_BACKEND", raising=False)
    rc = audit_typed_counters.main(
        store=object(),
        settings_factory=_Settings,
        invariant_audit=_report_func(_Report(True, "invariants clean")),
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "audit_typed_invariants: invariants clean" in out


def test_audit_script_exits_one_on_invariant_violation(capsys: pytest.CaptureFixture[str]) -> None:
    rc = audit_typed_counters.main(
        store=object(),
        settings_factory=_Settings,
        invariant_audit=_report_func(
            _Report(False, "invariants bad", {"credit:ws:0": {"typed_reserved": 10}})
        ),
    )

    assert rc == 1
    assert "VIOLATION credit:ws:0" in capsys.readouterr().out

def test_audit_script_refuses_non_spanner_bigtable_backend(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TR_STORAGE_BACKEND", "memory")

    rc = audit_typed_counters.main(
        store=object(),
        invariant_audit=_report_func(_Report(True, "unused")),
    )

    captured = capsys.readouterr()
    assert rc == 2
    assert "refusing to audit" in captured.err
    assert "memory" in captured.err


def test_audit_script_exits_two_on_infrastructure_error(capsys: pytest.CaptureFixture[str]) -> None:
    def broken_audit(_store: Any, *, max_samples: int = 20) -> _Report:
        raise RuntimeError("credentials unavailable")

    rc = audit_typed_counters.main(
        store=object(),
        settings_factory=_Settings,
        invariant_audit=broken_audit,
    )

    captured = capsys.readouterr()
    assert rc == 2
    assert "infrastructure failure" in captured.err
