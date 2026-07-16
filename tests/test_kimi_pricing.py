from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import check_price_coverage
from scripts.pricing.parsers import kimi as kimi_parser
from scripts.pricing.providers import kimi


def _pricing_doc(*, include_k3: bool = True, include_ghost: bool = False) -> str:
    rows = [
        '["kimi-k2.6", "1M tokens", <>{"$"}0.16</>, <>{"$"}0.95</>, '
        '<>{"$"}4.00</>, "262,144 tokens"]',
        '["kimi-k2.7-code", "1M tokens", <>{"$"}0.19</>, <>{"$"}0.95</>, '
        '<>{"$"}4.00</>, "262,144 tokens"]',
        '["kimi-k2.7-code-highspeed", "1M tokens", <>{"$"}0.38</>, '
        '<>{"$"}1.90</>, <>{"$"}8.00</>, "262,144 tokens"]',
    ]
    if include_k3:
        rows.append(
            '["kimi-k3", "1M tokens", <>{"$"}0.30</>, <>{"$"}3.00</>, '
            '<>{"$"}15.00</>, "1,048,576 tokens"]'
        )
    if include_ghost:
        rows.append(
            '["kimi-ghost", "1M tokens", <>{"$"}0.01</>, <>{"$"}0.01</>, '
            '<>{"$"}0.01</>, "1,024 tokens"]'
        )
    return "\n".join(rows)


def _live_payload(*, include_k3: bool = True) -> dict[str, Any]:
    ids = ["kimi-k2.6", "kimi-k2.7-code", "kimi-k2.7-code-highspeed"]
    if include_k3:
        ids.append("kimi-k3")
    return {
        "data": [
            {
                "id": model_id,
                "context_length": 1_048_576 if model_id == "kimi-k3" else 262_144,
                "supports_image_in": True,
                "supports_video_in": model_id != "kimi-k3",
                "supports_reasoning": True,
            }
            for model_id in ids
        ]
    }


def _fake_docs(url: str, *, extra_headers: dict[str, str] | None = None) -> str:
    del extra_headers
    if url == kimi.DOC_INDEX_URL:
        return "\n".join(
            [
                f"- [K3]({kimi.DOC_INDEX_URL.removesuffix('llms.txt')}pricing/chat-k3.md)",
                "- [Ignore](https://attacker.example/pricing/chat-k4.md)",
            ]
        )
    return _pricing_doc(include_k3=True, include_ghost=True)


def test_pricing_subpages_only_accepts_moonshot_docs_origin() -> None:
    k3_url = "https://platform.kimi.ai/docs/pricing/chat-k3.md"
    urls = kimi._pricing_subpages(  # noqa: SLF001
        f"[K3]({k3_url}) [duplicate]({k3_url}) "
        "[bad](https://attacker.example/docs/pricing/chat-k4.md)"
    )

    assert urls[0] == k3_url
    assert urls.count(k3_url) == 1
    assert all("attacker.example" not in url for url in urls)
    assert "https://platform.kimi.ai/docs/pricing/chat-k27-code.md" in urls


def test_parser_accepts_future_kimi_family_without_code_change() -> None:
    parsed = kimi_parser.parse(_pricing_doc(include_k3=True))

    assert parsed["moonshotai/kimi-k3"] == {
        "prompt_micro_per_m": 3_000_000,
        "completion_micro_per_m": 15_000_000,
        "prompt_cached_micro_per_m": 300_000,
    }


def test_fetch_intersects_live_models_and_writes_manifest(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "kimi.json"
    seen_headers: list[dict[str, str] | None] = []

    def fake_fetch_json(
        url: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        assert url == kimi.MODELS_URL
        seen_headers.append(extra_headers)
        return _live_payload(include_k3=True)

    monkeypatch.setenv("KIMI_API_KEY", "test-kimi-key")
    monkeypatch.setattr(kimi, "fetch_html", _fake_docs)
    monkeypatch.setattr(kimi, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(kimi, "MANIFEST_PATH", manifest_path)

    result = kimi.fetch()
    notes = kimi.write_provider_manifest(result)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in manifest["models"]}

    assert seen_headers == [{"Authorization": "Bearer test-kimi-key"}]
    assert "moonshotai/kimi-k3" in result.prices
    assert "moonshotai/kimi-ghost" not in result.prices
    assert by_id["moonshotai/kimi-k3"]["upstream_id"] == "kimi-k3"
    assert by_id["moonshotai/kimi-k3"]["context_length"] == 1_048_576
    assert by_id["moonshotai/kimi-k3"]["input_token_price_per_m"] == 3_000_000
    assert by_id["moonshotai/kimi-k3"]["output_token_price_per_m"] == 15_000_000
    assert by_id["moonshotai/kimi-k3"]["cached_input_token_price_per_m"] == 300_000
    assert notes == ["kimi: refreshed provider_models/kimi.json (4 priced rows, appended 4)"]


def test_live_model_without_first_party_price_is_not_published(
    monkeypatch: Any,
) -> None:
    def docs_without_k3(url: str, *, extra_headers: dict[str, str] | None = None) -> str:
        del extra_headers
        if url == kimi.DOC_INDEX_URL:
            return ""
        return _pricing_doc(include_k3=False)

    monkeypatch.setenv("KIMI_API_KEY", "test-kimi-key")
    monkeypatch.setattr(kimi, "fetch_html", docs_without_k3)
    monkeypatch.setattr(
        kimi,
        "fetch_json",
        lambda _url, **_kwargs: _live_payload(include_k3=True),
    )

    with pytest.raises(RuntimeError, match="expected models missing.*moonshotai/kimi-k3"):
        kimi.fetch()


def test_manifest_removes_models_no_longer_live_or_priced(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "kimi.json"
    manifest_path.write_text(
        json.dumps(
            {
                "provider": "kimi",
                "models": [
                    {
                        "id": "moonshotai/kimi-retired",
                        "upstream_id": "kimi-retired",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KIMI_API_KEY", "test-kimi-key")
    monkeypatch.setattr(kimi, "fetch_html", _fake_docs)
    monkeypatch.setattr(
        kimi,
        "fetch_json",
        lambda _url, **_kwargs: _live_payload(include_k3=True),
    )
    monkeypatch.setattr(kimi, "MANIFEST_PATH", manifest_path)

    result = kimi.fetch()
    kimi.write_provider_manifest(result)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert "moonshotai/kimi-retired" not in {row["id"] for row in manifest["models"]}


def test_missing_key_fails_closed_before_publishing_provider_routes(
    monkeypatch: Any,
) -> None:
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    monkeypatch.setattr(kimi, "fetch_html", _fake_docs)
    monkeypatch.setattr(
        kimi,
        "fetch_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected API call")),
    )

    with pytest.raises(RuntimeError, match="KIMI_API_KEY or MOONSHOT_API_KEY is required"):
        kimi.fetch()


def test_coverage_normalizes_kimi_native_ids() -> None:
    assert check_price_coverage._kimi_model_id("kimi-k3") == "moonshotai/kimi-k3"  # noqa: SLF001
    assert check_price_coverage._kimi_model_id(" KIMI-K2.7-CODE ") == (  # noqa: SLF001
        "moonshotai/kimi-k2.7-code"
    )
    assert check_price_coverage._kimi_model_id("unrelated-model") is None  # noqa: SLF001
