"""A tiered price row is not replaced by a single price for page-parsed providers."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.parsers import anthropic as parser
from scripts.pricing.providers import anthropic as provider

HAIKU = "anthropic/claude-haiku-5.5"
FIXTURES = Path(__file__).parent / "fixtures" / "pricing"


def _manifest_profile(tiered: bool, price: int) -> dict:
    profile = {
        "input_token_price_per_m": price,
        "output_token_price_per_m": price * 5,
        "cached_input_token_price_per_m": price // 10,
    }
    if tiered:
        profile["price_tiers"] = [
            {"max_prompt_tokens": 100_000, **profile},
            {"max_prompt_tokens": None, "input_token_price_per_m": price * 2,
             "output_token_price_per_m": price * 10, "cached_input_token_price_per_m": price // 5},
        ]
    return profile



def _new_price(tiered: bool) -> ModelPrice:
    from scripts.pricing.base import PriceTier as ParsedTier

    tiers = [ParsedTier(100_000 if tiered else None, 2_000_000, 10_000_000, 200_000)]
    if tiered:
        tiers.append(ParsedTier(None, 4_000_000, 20_000_000, 400_000))
    return ModelPrice(tiers=tiers)



@pytest.mark.parametrize("hold", [True, False])
@pytest.mark.parametrize("old_tiered,new_tiered", [(True, False), (True, True),
                                                  (False, True), (False, False)])
def test_shared_writer_price_transitions(
    tmp_path: Path, hold: bool, old_tiered: bool, new_tiered: bool,
) -> None:
    slug = "anthropic"
    from scripts.pricing import refresh
    from scripts.pricing.manifest import write_discovered_chat_manifest

    manifest = tmp_path / f"{slug}.json"
    row = {"id": HAIKU, "display_name": "Saved model", **_manifest_profile(old_tiered, 1_000_000)}
    raw = {"provider": slug, "models": [row]}
    manifest.write_text(json.dumps(raw, indent=2) + "\n")
    old_row_bytes = json.dumps(row, indent=2).encode()
    result = ProviderPricingResult(slug=slug, source="deterministic",
                                   prices={HAIKU: _new_price(new_tiered)})
    # Discovery attempts to replace prices too: committed prices win a hold.
    discovered = {HAIKU: {"id": HAIKU, "display_name": "Saved model",
                           **_manifest_profile(False, 9_000_000),
                           "price_tiers": [{"max_prompt_tokens": None, "input_token_price_per_m": 7,
                                            "output_token_price_per_m": 7}]}}
    write_discovered_chat_manifest(result, manifest_path=manifest,
                                   discovered_rows=discovered, source_url="https://example.test",
                                   hold_tier_removal=hold)
    actual = json.loads(manifest.read_text())["models"][0]
    if hold and old_tiered and not new_tiered:
        assert json.dumps(actual, indent=2).encode() == old_row_bytes
        note = (f"kept tiered prices for {HAIKU}: new parse returned a single price; "
                "if the tiers are really gone, remove price_tiers from the manifest by hand")
        assert result.notes == [note]
        assert result.price_holds == {HAIKU: note}
        # This is the structure printed under the alert workflow's heading.
        assert refresh._price_review_holds({slug: result}, {}) == {slug: [note]}
        assert note in "\n".join(refresh._summary_lines({slug: result}, [], [], [], []))
    else:
        assert actual == {"id": HAIKU, "display_name": "Saved model",
                          **_manifest_profile(new_tiered, 2_000_000)}
        assert result.notes == []
        assert result.price_holds == {}



@pytest.mark.parametrize("discovered", [True, False])
def test_shared_writer_missing_price_keeps_existing_behavior(tmp_path: Path, discovered: bool) -> None:
    from scripts.pricing.manifest import write_discovered_chat_manifest

    manifest = tmp_path / "test.json"
    row = {"id": HAIKU, **_manifest_profile(True, 1_000_000)}
    siblings = [{"id": f"example/sibling-{i}", **_manifest_profile(False, 1_000_000)}
                for i in range(3)]
    manifest.write_text(json.dumps({"provider": "anthropic", "models": [row, *siblings]}))
    result = ProviderPricingResult(slug="anthropic", source="deterministic",
                                   prices={sibling["id"]: _new_price(False) for sibling in siblings})
    discovery = {sibling["id"]: {"id": sibling["id"]} for sibling in siblings}
    if discovered:
        discovery[HAIKU] = {"id": HAIKU}
    write_discovered_chat_manifest(result, manifest_path=manifest,
                                   discovered_rows=discovery, source_url="https://example.test",
                                   hold_tier_removal=True)
    actual = json.loads(manifest.read_text())["models"][0]
    if discovered:
        assert actual == {"id": HAIKU, "routable": False, "routable_reason": "price-unavailable"}
    else:
        assert actual == row
    assert result.notes == []
    assert result.price_holds == {}



def test_tier_hold_reaches_refresh_alert_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from types import SimpleNamespace

    from scripts.pricing import refresh
    from scripts.pricing.manifest import write_discovered_chat_manifest

    manifests = tmp_path / "provider_models"
    manifests.mkdir()
    manifest = manifests / "anthropic.json"
    old = {"id": HAIKU, **_manifest_profile(True, 1_000_000)}
    manifest.write_text(json.dumps({"provider": "anthropic", "models": [old]}))
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"models": []}))
    parsers = tmp_path / "parsers"
    parsers.mkdir()
    result = ProviderPricingResult(slug="anthropic", source="deterministic",
                                   prices={HAIKU: _new_price(False)},
                                   notes=["prior note"] * 4)

    def write(res: ProviderPricingResult) -> list[str]:
        return write_discovered_chat_manifest(
            res, manifest_path=manifest, discovered_rows={HAIKU: {"id": HAIKU}},
            source_url="https://example.test", hold_tier_removal=True,
        )

    module = SimpleNamespace(MANIFEST_PATH=manifest, write_provider_manifest=write)
    monkeypatch.setattr(refresh, "SNAPSHOT_PATH", snapshot)
    monkeypatch.setattr(refresh, "PROVIDER_MANIFEST_DIR", manifests)
    monkeypatch.setattr(refresh, "PARSERS_DIR", parsers)
    monkeypatch.setattr(refresh, "parser_path", lambda slug: parsers / f"{slug}.py")
    monkeypatch.setattr(refresh, "PROVIDER_SLUGS", ("anthropic",))
    monkeypatch.setattr(refresh, "_import_provider", lambda slug: module)
    monkeypatch.setattr(refresh, "_fetch_all_providers", lambda: ({"anthropic": result}, []))
    monkeypatch.setattr(refresh, "build_openrouter_snapshot", lambda: {"models": []})
    monkeypatch.setattr(refresh, "_new_parser_requirements", lambda *_args: {})
    monkeypatch.setattr(refresh, "configure_runtime_required_models", lambda *_args: None)
    monkeypatch.setattr(refresh, "quarantined_providers", lambda: {})
    assert refresh.main([]) == 0
    output = capsys.readouterr().out
    # The workflow uses sed from this heading to the next blank line. Check
    # the actual emitted section even when ordinary notes are truncated.
    alert_section = output.split(refresh.HELD_FOR_REVIEW_HEADING + "\n", 1)[1].split("\n\n", 1)[0]
    assert result.price_holds[HAIKU] in alert_section
    assert json.loads(manifest.read_text())["models"][0] == old



@pytest.mark.parametrize("reason", ["provider-canary-failed", "operator-safety-hold"])
def test_price_hold_does_not_override_discovery_or_operator_safety_holds(
    tmp_path: Path, reason: str,
) -> None:
    from scripts.pricing.manifest import write_discovered_chat_manifest

    manifest = tmp_path / "test.json"
    profile = _manifest_profile(True, 1_000_000)
    rows = [{"id": HAIKU, **profile},
            *[{"id": f"example/sibling-{i}", **_manifest_profile(False, 1_000_000)}
              for i in range(3)]]
    manifest.write_text(json.dumps({"provider": "anthropic", "models": rows}))
    discovered = {row["id"]: {"id": row["id"]} for row in rows}
    if reason == "provider-canary-failed":
        discovered[HAIKU].update(routable=False, routable_reason=reason)
    result = ProviderPricingResult(slug="anthropic", source="api",
                                   prices={row["id"]: _new_price(False) for row in rows})
    write_discovered_chat_manifest(
        result, manifest_path=manifest, discovered_rows=discovered,
        source_url="https://example.test", hold_tier_removal=True,
        operator_hold_reasons={HAIKU: reason} if reason == "operator-safety-hold" else None,
    )
    actual = json.loads(manifest.read_text())["models"][0]
    assert actual == {"id": HAIKU, **profile, "routable": False, "routable_reason": reason}



def test_captured_fixture_old_rows_unchanged_and_new_tiers_exact() -> None:
    expected = json.loads((FIXTURES / "anthropic.expected.json").read_text())
    actual = parser.parse((FIXTURES / "anthropic.html").read_text())
    assert len(expected) == 14
    assert len(expected[HAIKU]["tiers"]) == 2
    # Other 13 rows are frozen pre-change output, not checked-in manifest prices.
    assert {k: v for k, v in actual.items() if k != HAIKU} == {
        k: v for k, v in expected.items() if k != HAIKU
    }
    assert actual == expected



def test_self_heal_fixture_validation_rejects_flat_tier_stub() -> None:
    from scripts.pricing.base import _assert_fixture_compatibility

    expected = json.loads((FIXTURES / "anthropic.expected.json").read_text())
    # Run the actual sandbox and compatibility check, not a stand-in validator.
    _assert_fixture_compatibility(
        slug="anthropic", current_parse_fn=parser.parse,
        candidate_source=f"def parse(html):\n    return {expected!r}\n",
    )
    expected[HAIKU] = {key: value for key, value in expected[HAIKU]["tiers"][0].items()
                       if key != "max_prompt_tokens"}
    with pytest.raises(RuntimeError, match=r"self-heal fixture regression:.*changed=.*haiku-5.5"):
        _assert_fixture_compatibility(
            slug="anthropic", current_parse_fn=parser.parse,
            candidate_source=f"def parse(html):\n    return {expected!r}\n",
        )



def test_a_route_returning_from_delisting_takes_the_new_price(tmp_path: Path) -> None:
    # A delisted row can be months old. Its tiers are not evidence about
    # today's prices, so the fresh single price is published.
    from scripts.pricing.manifest import write_discovered_chat_manifest

    manifest = tmp_path / "anthropic.json"
    row = {"id": HAIKU, "display_name": "Saved model", **_manifest_profile(True, 1_000_000),
           "routable": False, "routable_reason": "delisted-upstream"}
    manifest.write_text(json.dumps({"provider": "anthropic", "models": [row]}))
    result = ProviderPricingResult(slug="anthropic", source="deterministic",
                                   prices={HAIKU: _new_price(False)})
    write_discovered_chat_manifest(result, manifest_path=manifest,
                                   discovered_rows={HAIKU: {"id": HAIKU, "display_name": "Saved model"}},
                                   source_url="https://example.test", hold_tier_removal=True)
    actual = json.loads(manifest.read_text())["models"][0]
    assert "price_tiers" not in actual
    assert actual["input_token_price_per_m"] == 2_000_000
    assert result.price_holds == {}


def test_the_hold_repeats_on_every_refresh_until_released_by_hand(tmp_path: Path) -> None:
    from scripts.pricing.manifest import write_discovered_chat_manifest

    manifest = tmp_path / "anthropic.json"
    row = {"id": HAIKU, "display_name": "Saved model", **_manifest_profile(True, 1_000_000)}
    manifest.write_text(json.dumps({"provider": "anthropic", "models": [row]}))
    for _ in range(3):
        result = ProviderPricingResult(slug="anthropic", source="deterministic",
                                       prices={HAIKU: _new_price(False)})
        write_discovered_chat_manifest(result, manifest_path=manifest,
                                       discovered_rows={HAIKU: {"id": HAIKU, "display_name": "Saved model"}},
                                       source_url="https://example.test", hold_tier_removal=True)
        assert json.loads(manifest.read_text())["models"][0] == row
        assert list(result.price_holds) == [HAIKU]
    # Release: a person removes the tiers; the next refresh publishes the single price.
    released = {k: v for k, v in row.items() if k != "price_tiers"}
    manifest.write_text(json.dumps({"provider": "anthropic", "models": [released]}))
    result = ProviderPricingResult(slug="anthropic", source="deterministic",
                                   prices={HAIKU: _new_price(False)})
    write_discovered_chat_manifest(result, manifest_path=manifest,
                                   discovered_rows={HAIKU: {"id": HAIKU, "display_name": "Saved model"}},
                                   source_url="https://example.test", hold_tier_removal=True)
    assert json.loads(manifest.read_text())["models"][0]["input_token_price_per_m"] == 2_000_000
    assert result.price_holds == {}


def test_the_anthropic_provider_opts_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def capture(result: ProviderPricingResult, **kwargs: object) -> list[str]:
        seen.update(kwargs)
        return []

    monkeypatch.setattr(provider, "write_discovered_chat_manifest", capture)
    provider.write_provider_manifest(
        ProviderPricingResult(slug="anthropic", source="deterministic", prices={}))
    assert seen["hold_tier_removal"] is True


def test_the_hold_is_off_unless_a_provider_asks_for_it() -> None:
    import inspect

    from scripts.pricing.manifest import write_discovered_chat_manifest

    assert inspect.signature(write_discovered_chat_manifest).parameters["hold_tier_removal"].default is False
