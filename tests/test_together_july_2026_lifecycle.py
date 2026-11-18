from __future__ import annotations

from datetime import UTC, datetime

from scripts.pricing.providers import together
from trusted_router import provider_lifecycle

_CUTOFF = datetime(2026, 7, 27, 0, 0, tzinfo=UTC)
_M27 = "minimax/minimax-m2.7"
_M27_UPSTREAM = "MiniMaxAI/MiniMax-M2.7"
_M3 = "minimax/minimax-m3"
_M3_UPSTREAM = "MiniMaxAI/MiniMax-M3"


def test_together_minimax_m27_retirement_spares_m3() -> None:
    # The cutoff itself, scope and the refresh and catalog paths are checked
    # for every retirement in tests/test_provider_retirements_generic.py.
    assert not provider_lifecycle.provider_model_retired(
        "together",
        _M3,
        _M3_UPSTREAM,
        at=_CUTOFF,
    )


def test_together_parser_pins_replacement_and_retiring_native_ids() -> None:
    assert together.UPSTREAM_ID_MAP[_M27] == _M27_UPSTREAM
    assert together.UPSTREAM_ID_MAP[_M3] == _M3_UPSTREAM
    assert _M3 in together.EXPECTED_MODELS
