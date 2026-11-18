from __future__ import annotations

import json
from datetime import UTC, datetime

from scripts.pricing.providers import friendli
from trusted_router import provider_lifecycle

_CUTOFF = datetime(2026, 10, 22, tzinfo=UTC)
_MODEL = "minimax/minimax-m2.5"
_NATIVE = "MiniMaxAI/MiniMax-M2.5"
_OTHER = "z-ai/glm-5.3"


def test_friendli_october_cutoffs_are_the_announced_dates_and_spare_glm() -> None:
    # The cutoffs themselves, scope and the refresh and catalog paths are
    # checked for every retirement in tests/test_provider_retirements_generic.py.
    assert provider_lifecycle.FRIENDLI_MINIMAX_M25_RETIREMENT_AT == _CUTOFF
    assert provider_lifecycle.FRIENDLI_DEEPSEEK_V32_RETIREMENT_AT == _V32_CUTOFF
    for cutoff in (_CUTOFF, _V32_CUTOFF):
        assert not provider_lifecycle.provider_model_retired(
            "friendli", _OTHER, "zai-org/GLM-5.3", at=cutoff,
        )


def test_manifest_records_the_announced_retirement() -> None:
    rows = {row["id"]: row for row in json.loads(friendli.MANIFEST_PATH.read_text())["models"]}
    assert rows[_MODEL]["upstream_id"] == _NATIVE
    assert rows[_MODEL]["retirement_at"] == "2026-10-22T00:00:00Z"
    assert "replacement_model_id" not in rows[_MODEL]
    assert "retirement_at" not in rows[_OTHER]


_V32_CUTOFF = datetime(2026, 10, 24, tzinfo=UTC)
_V32 = "deepseek/deepseek-v3.2"
_V32_NATIVE = "deepseek-ai/DeepSeek-V3.2"


def test_friendli_deepseek_v32_manifest_records_the_retirement() -> None:
    rows = {row["id"]: row for row in json.loads(friendli.MANIFEST_PATH.read_text())["models"]}
    assert rows[_V32]["upstream_id"] == _V32_NATIVE
    assert rows[_V32]["retirement_at"] == "2026-10-24T00:00:00Z"
