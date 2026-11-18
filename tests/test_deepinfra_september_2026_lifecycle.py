from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from scripts.pricing.providers import deepinfra
from trusted_router import provider_lifecycle


@dataclass(frozen=True)
class RetirementCase:
    model: str
    upstream: str
    replacement: str
    replacement_upstream: str
    cutoff: datetime


@pytest.fixture(params=[
    RetirementCase(
        "moonshotai/kimi-k2.5", "moonshotai/Kimi-K2.5",
        "moonshotai/kimi-k3", "moonshotai/Kimi-K3",
        datetime(2026, 9, 7, tzinfo=UTC),
    ),
    RetirementCase(
        "z-ai/glm-4.7-flash", "zai-org/GLM-4.7-Flash",
        "z-ai/glm-5.3-flash", "zai-org/GLM-5.3-Flash",
        datetime(2026, 9, 10, tzinfo=UTC),
    ),
    RetirementCase(
        "z-ai/glm-5", "zai-org/GLM-5",
        "z-ai/glm-5.2", "zai-org/GLM-5.2",
        datetime(2026, 9, 10, tzinfo=UTC),
    ),
    RetirementCase(
        "minimax/minimax-m2.7", "MiniMaxAI/MiniMax-M2.7",
        "minimax/minimax-m3", "MiniMaxAI/MiniMax-M3",
        datetime(2026, 9, 10, tzinfo=UTC),
    ),
    RetirementCase(
        "xiaomi/mimo-v2.5", "XiaomiMiMo/MiMo-V2.5",
        "xiaomi/mimo-v2.6-flash", "XiaomiMiMo/MiMo-V2.6-Flash",
        datetime(2026, 9, 29, tzinfo=UTC),
    ),
], ids=lambda case: case.model)
def retirement(request: pytest.FixtureRequest) -> RetirementCase:
    return request.param


def test_deepinfra_september_replacement_is_not_retired(retirement: RetirementCase) -> None:
    # The cutoff itself, scope and the refresh and catalog paths are checked
    # for every retirement in tests/test_provider_retirements_generic.py.
    case = retirement
    assert not provider_lifecycle.provider_model_retired(
        "deepinfra", case.replacement, case.replacement_upstream, at=case.cutoff,
    )


@pytest.mark.parametrize("after_cutoff", [False, True])
def test_deepinfra_parser_and_manifest_filter_stale_feed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    retirement: RetirementCase, after_cutoff: bool,
) -> None:
    case = retirement
    payload = {"data": [
        {"id": native, "metadata": {"pricing": {"input_tokens": 0.45, "output_tokens": 2.25}}}
        for native in (case.upstream, case.replacement_upstream)
    ]}
    monkeypatch.setattr(
        deepinfra.httpx, "HTTPTransport",
        lambda **_: httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )
    monkeypatch.setattr(deepinfra, "UPSTREAM_ID_MAP", dict(deepinfra.UPSTREAM_ID_MAP))
    monkeypatch.setattr(deepinfra, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setattr(deepinfra, "MANIFEST_PATH", tmp_path / "deepinfra.json")
    monkeypatch.setattr(
        provider_lifecycle, "_utc_now",
        lambda: case.cutoff if after_cutoff else case.cutoff - timedelta(microseconds=1),
    )
    result = deepinfra.fetch()
    expected = {case.replacement}
    if not after_cutoff:
        expected.add(case.model)
        assert deepinfra._DISCOVERED_MANIFEST_ROWS[case.model]["upstream_id"] == case.upstream
    assert set(result.prices) == expected
    assert set(deepinfra._DISCOVERED_MANIFEST_ROWS) == expected
    deepinfra.write_provider_manifest(result)
    rows = json.loads(deepinfra.MANIFEST_PATH.read_text())["models"]
    assert {row["id"] for row in rows} == expected


def test_deepinfra_manifest_records_announced_replacement(retirement: RetirementCase) -> None:
    case = retirement
    rows = {row["id"]: row for row in json.loads(deepinfra.MANIFEST_PATH.read_text())["models"]}
    assert rows[case.model]["retirement_at"] == case.cutoff.isoformat().replace("+00:00", "Z")
    assert rows[case.model]["replacement_model_id"] == case.replacement
    assert rows[case.model]["upstream_id"] == case.upstream


def test_deepinfra_retirement_does_not_include_unannounced_turbo_variant() -> None:
    assert not provider_lifecycle.provider_model_retired(
        "deepinfra", "minimax/minimax-m2.7-turbo", "MiniMaxAI/MiniMax-M2.7-Turbo",
        at=datetime(2026, 9, 10, tzinfo=UTC),
    )


def test_deepinfra_mimo_retirement_covers_legacy_id_but_not_pro() -> None:
    cutoff = datetime(2026, 9, 29, tzinfo=UTC)
    retired = provider_lifecycle.provider_model_retired
    assert not retired(
        "deepinfra", "xiaomimimo/mimo-v2.5", at=cutoff - timedelta(microseconds=1),
    )
    assert retired("deepinfra", "xiaomimimo/mimo-v2.5", at=cutoff)
    assert not retired(
        "deepinfra", "xiaomi/mimo-v2.5-pro", "XiaomiMiMo/MiMo-V2.5-Pro", at=cutoff,
    )
    rows = {row["id"]: row for row in json.loads(deepinfra.MANIFEST_PATH.read_text())["models"]}
    assert rows["xiaomimimo/mimo-v2.5"]["retirement_at"] == "2026-09-29T00:00:00Z"
    assert rows["xiaomimimo/mimo-v2.5"]["replacement_model_id"] == "xiaomi/mimo-v2.6-flash"
