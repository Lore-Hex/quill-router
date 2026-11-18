from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from scripts.pricing.providers import together
from trusted_router import provider_lifecycle

_CUTOFF = datetime(2026, 10, 22, tzinfo=UTC)
_RETIRING = {
    "meta-llama/llama-3.3-70b-instruct": (
        "meta-llama/Llama-3.3-70B-Instruct-Turbo", "meta-models/muse-glimmer-30b",
    ),
    "deepseek/deepseek-v4-flash-0731": (
        "deepseek-ai/DeepSeek-V4-Flash-0731", "deepseek/deepseek-v4.1-flash",
    ),
}


def test_together_october_cutoff_is_midnight_utc() -> None:
    assert provider_lifecycle.TOGETHER_OCTOBER_22_RETIREMENT_AT == _CUTOFF


@pytest.mark.parametrize("model", sorted(_RETIRING))
def test_together_replacements_are_quoted_not_substituted(model: str) -> None:
    _, replacement = _RETIRING[model]
    entries = [
        entry for entry in provider_lifecycle.provider_retirements()
        if entry.provider == "together" and model in entry.model_ids
        and entry.effective_at == _CUTOFF
    ]
    assert len(entries) == 1
    assert entries[0].replacement_model_ids == (replacement,)
    assert not provider_lifecycle.provider_model_retired("together", replacement, at=_CUTOFF)


def test_together_manifest_records_the_announced_retirements() -> None:
    rows = {row["id"]: row for row in json.loads(together.MANIFEST_PATH.read_text())["models"]}
    for model, (native, replacement) in _RETIRING.items():
        if model not in rows:
            continue
        assert rows[model]["upstream_id"] == native
        assert rows[model]["retirement_at"] == "2026-10-22T00:00:00Z"
        assert rows[model]["replacement_model_id"] == replacement
