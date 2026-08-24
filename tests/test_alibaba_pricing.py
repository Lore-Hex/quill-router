from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from scripts.pricing import refresh
from scripts.pricing.providers import alibaba
from trusted_router import provider_lifecycle


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
        {"id": "deepseek-v4-flash-0731", "created": 4},
        {"id": "deepseek-v4-pro", "created": 5},
        {"id": "qwen3.7-flash", "created": 6},
        {"id": "qwen3.7-flash-2026-07-15", "created": 7},
        {"id": "qwen3.7-max", "created": 8},
        {"id": "qwen3.7-plus", "created": 9},
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


def test_deepseek_v4_flash_family_uses_published_ga_price() -> None:
    for upstream_id in (
        "deepseek-v4-flash",
        "deepseek-v4-flash-us",
        "deepseek-v4-flash-0731",
        "deepseek-v4-flash-0731-us",
    ):
        price = alibaba._price(upstream_id)  # noqa: SLF001

        assert price is not None
        assert price.prompt_micro_per_m == 138_000
        assert price.completion_micro_per_m == 275_000
        assert price.tiers[0].prompt_cached_micro_per_m == 28_000


def test_deepseek_preview_manifest_rows_name_the_dated_replacements() -> None:
    global_row = alibaba._manifest_row(  # noqa: SLF001
        model_id="deepseek/deepseek-v4-flash",
        native_id="deepseek-v4-flash",
        source_row={},
    )
    us_row = alibaba._manifest_row(  # noqa: SLF001
        model_id="deepseek/deepseek-v4-flash-us",
        native_id="deepseek-v4-flash-us",
        source_row={},
    )

    assert global_row["retirement_at"] == "2026-10-09T16:00:00Z"
    assert global_row["replacement_model_id"] == (
        "deepseek/deepseek-v4-flash-0731"
    )
    assert us_row["retirement_at"] == "2026-10-09T16:00:00Z"
    assert us_row["replacement_model_id"] == (
        "deepseek/deepseek-v4-flash-0731-us"
    )


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
    assert alibaba._DISCOVERED_MANIFEST_ROWS[  # noqa: SLF001
        "deepseek/deepseek-v4-flash-0731"
    ]["context_length"] == 1_000_000


def test_parser_filters_retired_deepseek_preview_from_stale_feed(
    monkeypatch,
) -> None:  # noqa: ANN001
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
    cutoff = provider_lifecycle.ALIBABA_OCTOBER_2026_RETIREMENT_AT
    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: cutoff - timedelta(microseconds=1),
    )

    before = alibaba.fetch()
    assert "deepseek/deepseek-v4-flash" in before.prices
    assert "deepseek/deepseek-v4-flash-0731" in before.prices

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: cutoff)
    after = alibaba.fetch()

    assert "deepseek/deepseek-v4-flash" not in after.prices
    assert "deepseek/deepseek-v4-flash" not in alibaba._DISCOVERED_MANIFEST_ROWS
    assert "deepseek/deepseek-v4-flash-0731" in after.prices


def test_hourly_refresh_cannot_restore_retired_deepseek_preview(
    monkeypatch,
) -> None:  # noqa: ANN001
    preview = "deepseek/deepseek-v4-flash"
    replacement = "deepseek/deepseek-v4-flash-0731"
    price = alibaba._price("deepseek-v4-flash-0731")  # noqa: SLF001
    assert price is not None
    result = alibaba.ProviderPricingResult(
        slug="alibaba",
        prices={preview: price, replacement: price},
        source="api",
        fetched_url=alibaba.URL,
    )
    cutoff = provider_lifecycle.ALIBABA_OCTOBER_2026_RETIREMENT_AT
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: cutoff)

    indexed = refresh._index_provider_prices({"alibaba": result})

    assert preview not in indexed
    assert "alibaba" in indexed[replacement]


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
    existing_priced_ids = {"qwen/qwen3.7-plus"}
    appended_count = len(set(result.prices) - existing_priced_ids)
    assert notes == [
        "alibaba: refreshed provider_models/alibaba.json "
        f"({len(result.prices)} priced rows, appended {appended_count})"
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
