from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from scripts.pricing.providers import openai
from trusted_router import provider_lifecycle

_CUTOFF = datetime(2026, 10, 23, tzinfo=UTC)
_RETIRING = {
    "openai/gpt-3.5-turbo": "gpt-3.5-turbo",
    "openai/gpt-3.5-turbo-completions": "gpt-3.5-turbo-completions",
    "openai/gpt-4": "gpt-4",
    "openai/gpt-4-0613-completions": "gpt-4-0613-completions",
    "openai/gpt-4-completions": "gpt-4-completions",
    "openai/gpt-4-1106-preview": "gpt-4-1106-preview",
    "openai/gpt-4-turbo-2024-04-09": "gpt-4-turbo-2024-04-09",
    "openai/gpt-4-turbo-completions": "gpt-4-turbo-completions",
    "openai/gpt-4.1-nano-2025-04-14": "gpt-4.1-nano-2025-04-14",
    "openai/gpt-image-1": "gpt-image-1",
    "openai/ft-o4-mini-2025-04-16": "ft-o4-mini-2025-04-16",
    "openai/gpt-3.5-turbo-0125": "gpt-3.5-turbo-0125",
    "openai/gpt-4-0613": "gpt-4-0613",
    "openai/gpt-4-turbo": "gpt-4-turbo",
    "openai/gpt-4.1-nano": "gpt-4.1-nano",
    "openai/gpt-4o-2024-05-13": "gpt-4o-2024-05-13",
    "openai/o1": "o1",
    "openai/o1-pro": "o1-pro",
    "openai/o3-mini": "o3-mini",
    "openai/o4-mini": "o4-mini",
}
_RETAINED = {
    "openai/gpt-4.1": "gpt-4.1",
    "openai/gpt-4.1-mini": "gpt-4.1-mini",
    "openai/gpt-4o": "gpt-4o",
    "openai/gpt-4o-2024-08-06": "gpt-4o-2024-08-06",
    "openai/o3": "o3",
}


def test_openai_october_cutoff_is_midnight_utc() -> None:
    assert provider_lifecycle.OPENAI_OCTOBER_23_RETIREMENT_AT == _CUTOFF


@pytest.mark.parametrize(("model", "native"), _RETAINED.items())
def test_openai_unnamed_models_and_aliases_survive(model: str, native: str) -> None:
    assert not provider_lifecycle.provider_model_retired("openai", model, native, at=_CUTOFF)


def test_openai_manifest_records_the_announced_retirements() -> None:
    rows = {row["id"]: row for row in json.loads(openai.MANIFEST_PATH.read_text())["models"]}
    for model in ("openai/gpt-3.5-turbo-0125", "openai/gpt-4-0613", "openai/gpt-4.1-nano"):
        assert rows[model]["retirement_at"] == "2026-10-23T00:00:00Z"
    assert "retirement_at" not in rows["openai/gpt-4.1-mini"]
