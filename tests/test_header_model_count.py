"""The header's model count renders on every public page, so it must fail soft."""

from __future__ import annotations

import logging

import pytest

from trusted_router import homepage
from trusted_router.routes import catalog


def test_live_model_count_falls_back_to_zero_and_logs_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken() -> object:
        raise RuntimeError("catalog cannot be built")

    monkeypatch.setattr(catalog, "_current_catalog_payload", broken)
    monkeypatch.setattr(homepage, "_model_count_failure_logged", False)
    with caplog.at_level(logging.ERROR, logger="trusted_router.homepage"):
        assert homepage.live_model_count() == 0
        assert homepage.live_model_count() == 0
    tracebacks = [r for r in caplog.records if "header model count unavailable" in r.getMessage()]
    assert len(tracebacks) == 1, "one traceback per outage, not one per page view"


def test_live_model_count_recovers_and_logs_again_on_the_next_outage(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class Payload:
        shapes = [object(), object(), object()]

    monkeypatch.setattr(homepage, "_model_count_failure_logged", True)
    monkeypatch.setattr(catalog, "_current_catalog_payload", lambda: Payload())
    assert homepage.live_model_count() == 3
    assert homepage._model_count_failure_logged is False
