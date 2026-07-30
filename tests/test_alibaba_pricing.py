from __future__ import annotations

import json
from pathlib import Path

from scripts.pricing import refresh
from scripts.pricing.providers import alibaba


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


def _expected_model_rows() -> list[dict[str, object]]:
    return [
        {"id": "glm-5.2", "created": 1},
        {"id": "kimi-k2.7-code", "created": 2},
        {"id": "deepseek-v4-flash", "created": 3},
        {"id": "deepseek-v4-pro", "created": 4},
        {"id": "qwen3.7-flash", "created": 5},
        {"id": "qwen3.7-flash-2026-07-15", "created": 6},
        {"id": "qwen3.7-max", "created": 7},
        {"id": "qwen3.7-plus", "created": 8},
    ]


def test_qwen_37_flash_uses_published_context_price_tiers() -> None:
    price = alibaba._price("qwen3.7-flash")  # noqa: SLF001

    assert price is not None
    assert [
        (
            tier.max_prompt_tokens,
            tier.prompt_micro_per_m,
            tier.completion_micro_per_m,
            tier.prompt_cached_micro_per_m,
        )
        for tier in price.tiers
    ] == [
        (32_000, 30_000, 130_000, 6_000),
        (256_000, 100_000, 400_000, 20_000),
        (None, 200_000, 800_000, 40_000),
    ]


def test_fetch_discovers_qwen_37_flash_alias_and_snapshot(monkeypatch) -> None:  # noqa: ANN001
    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # noqa: ANN003
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, *_args, **_kwargs) -> FakeResponse:  # noqa: ANN002, ANN003
            return FakeResponse({"data": _expected_model_rows()})

    monkeypatch.setattr(alibaba.httpx, "Client", FakeClient)

    result = alibaba.fetch()

    assert "qwen/qwen3.7-flash" in result.prices
    assert alibaba.UPSTREAM_ID_MAP["qwen/qwen3.7-flash"] == "qwen3.7-flash"
    assert (
        alibaba.UPSTREAM_ID_MAP["qwen/qwen3.7-flash-2026-07-15"]
        == "qwen3.7-flash-2026-07-15"
    )
    assert alibaba._DISCOVERED_MANIFEST_ROWS["qwen/qwen3.7-flash"][  # noqa: SLF001
        "context_length"
    ] == 1_048_576


def test_manifest_refresh_appends_new_models_with_tiered_prices(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    manifest_path = tmp_path / "alibaba.json"
    manifest_path.write_text(
        json.dumps(
            {
                "provider": "alibaba",
                "models": [
                    {
                        "id": "qwen/qwen3.7-plus",
                        "upstream_id": "qwen3.7-plus",
                        "display_name": "Qwen3.7 Plus",
                        "endpoints": ["chat/completions"],
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                        "price_tiers": [
                            {
                                "max_prompt_tokens": 256_000,
                                "input_token_price_per_m": 400_000,
                                "output_token_price_per_m": 1_600_000,
                            },
                            {
                                "max_prompt_tokens": None,
                                "input_token_price_per_m": 1_200_000,
                                "output_token_price_per_m": 4_800_000,
                            },
                        ],
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # noqa: ANN003
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, *_args, **_kwargs) -> FakeResponse:  # noqa: ANN002, ANN003
            return FakeResponse({"data": _expected_model_rows()})

    monkeypatch.setattr(alibaba, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(alibaba.httpx, "Client", FakeClient)

    result = alibaba.fetch()
    notes = alibaba.write_provider_manifest(result)

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in raw["models"]}
    flash = by_id["qwen/qwen3.7-flash"]
    assert notes == [
        "alibaba: refreshed provider_models/alibaba.json "
        "(8 priced rows, appended 7)"
    ]
    assert flash["upstream_id"] == "qwen3.7-flash"
    assert flash["input_token_price_per_m"] == 30_000
    assert flash["output_token_price_per_m"] == 130_000
    assert flash["price_tiers"][-1] == {
        "max_prompt_tokens": None,
        "input_token_price_per_m": 200_000,
        "output_token_price_per_m": 800_000,
        "cached_input_token_price_per_m": 40_000,
    }
    assert by_id["qwen/qwen3.7-plus"]["display_name"] == "Qwen3.7 Plus"
    assert len(by_id["qwen/qwen3.7-plus"]["price_tiers"]) == 2


def test_unknown_live_model_is_discovered_but_held_until_priced(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    manifest_path = tmp_path / "alibaba.json"
    manifest_path.write_text(
        json.dumps({"provider": "alibaba", "models": []}) + "\n",
        encoding="utf-8",
    )

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # noqa: ANN003
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, *_args, **_kwargs) -> FakeResponse:  # noqa: ANN002, ANN003
            return FakeResponse(
                {
                    "data": [
                        *_expected_model_rows(),
                        {"id": "new-frontier-model", "created": 9},
                    ]
                }
            )

    monkeypatch.setattr(alibaba, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(alibaba.httpx, "Client", FakeClient)

    result = alibaba.fetch()
    alibaba.write_provider_manifest(result)

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in raw["models"]}
    unresolved = by_id["alibaba/new-frontier-model"]
    assert unresolved["upstream_id"] == "new-frontier-model"
    assert unresolved["routable"] is False
    assert unresolved["routable_reason"] == "awaiting-price"
    assert unresolved["unresolved_since"]
    assert "input_token_price_per_m" not in unresolved
    assert "output_token_price_per_m" not in unresolved


def test_alibaba_is_wired_to_hourly_refresh() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "refresh-prices.yml"
    ).read_text(encoding="utf-8")

    assert "alibaba" in refresh.PROVIDER_SLUGS
    assert 'cron: "0 * * * *"' in workflow
    assert "ALIBABA_API_KEY:trustedrouter-alibaba-api-key" in workflow
