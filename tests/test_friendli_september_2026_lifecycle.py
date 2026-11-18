from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from scripts.pricing.providers import friendli
from trusted_router import provider_lifecycle

_CUTOFF = datetime(2026, 9, 6, tzinfo=UTC)
_MODEL = "lgai-exaone/k-exaone-2.0-750b-a37b"
_NATIVE = "LGAI-EXAONE/K-EXAONE-2.0-750B-A37B"
_OTHER = "z-ai/glm-5.2"


def test_friendli_exaone_2_retirement_spares_glm() -> None:
    # The cutoff itself, scope and the refresh and catalog paths are checked
    # for every retirement in tests/test_provider_retirements_generic.py.
    assert not provider_lifecycle.provider_model_retired(
        "friendli", _OTHER, "zai-org/GLM-5.2", at=_CUTOFF,
    )


@pytest.mark.parametrize("after_cutoff", [False, True])
def test_discovery_filters_stale_feed(
    monkeypatch: pytest.MonkeyPatch, after_cutoff: bool,
) -> None:
    payload = {"data": [
        {"id": native, "pricing": {"input": "0.0000006", "output": "0.0000024"}}
        for native in (_NATIVE, "zai-org/GLM-5.2")
    ]}
    monkeypatch.setattr(
        friendli.httpx, "HTTPTransport",
        lambda **_: httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )
    monkeypatch.setattr(friendli, "UPSTREAM_ID_MAP", dict(friendli.UPSTREAM_ID_MAP))
    monkeypatch.setattr(friendli, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setattr(
        provider_lifecycle, "_utc_now",
        lambda: _CUTOFF if after_cutoff else _CUTOFF - timedelta(microseconds=1),
    )
    result = friendli.fetch()
    expected = {_OTHER} if after_cutoff else {_OTHER, _MODEL}
    assert set(result.prices) == expected
    assert set(friendli._DISCOVERED_MANIFEST_ROWS) == expected
