from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import (
    ProviderPricingResult,
    _coerce_to_model_prices,
    guard_manifest_prune,
    normalize_parser_input,
)
from scripts.pricing.parsers import minimax as parser
from scripts.pricing.providers import minimax as provider

LOW = r"> | **MiniMax-M3** \u2264 512k input tokens Permanent 50% off | ~~\$0.64~~ \$0.32 | ~~\$2.56~~ \$1.28 | ~~\$0.128~~ \$0.064 |".replace(r"\u2264", "\u2264")
HIGH = r"> | **MiniMax-M3** > 512k input tokens Permanent 50% off | ~~\$1.28~~ \$0.64 | ~~\$5.12~~ \$2.56 | ~~\$0.256~~ \$0.128 |"
PRIORITY = "> [tab] Priority*\n" + LOW.replace("0.32", "0.48") + "\n" + HIGH.replace("0.64", "0.96")
FLASH = r"| **MiniMax-M3.1-Flash** | \$0.17 | \$0.68 | \$0.017 |"


def _standard(*rows: str) -> str:
    return "> [tab] Standard\n>\n> | Model | Input | Output | Cache |\n" + "\n".join(rows)


@pytest.mark.parametrize("priority_first", [False, True])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_blockquoted_standard_tiers_never_use_priority_prices(
    priority_first: bool, newline: str,
) -> None:
    sections = [_standard(LOW, HIGH), PRIORITY]
    if priority_first:
        sections.reverse()
    page = (FLASH + "\n\n" + "\n\n".join(sections) + "\n\n## Video\n").replace("\n", newline)
    prices, errors = _coerce_to_model_prices(parser.parse(normalize_parser_input(page)))
    assert not errors and prices is not None
    tiers = prices["minimax/minimax-m3"].tiers
    assert [(t.max_prompt_tokens, t.prompt_micro_per_m, t.completion_micro_per_m,
             t.prompt_cached_micro_per_m) for t in tiers] == [
        (512_000, 320_000, 1_280_000, 64_000),
        (None, 640_000, 2_560_000, 128_000),
    ]
    assert prices["minimax/minimax-m3.1-flash"].prompt_micro_per_m == 170_000


@pytest.mark.parametrize("page", [
    PRIORITY,
    _standard(LOW) + "\n" + PRIORITY,
    _standard(HIGH) + "\n" + PRIORITY,
    _standard(LOW, LOW),
    _standard(HIGH, HIGH),
    _standard(LOW, HIGH, HIGH),
    _standard(LOW.replace("512k", "256k"), HIGH),
    _standard(LOW, HIGH.replace("512k", "1024k")),
    _standard(LOW, HIGH.replace("MiniMax-M3", "MiniMax-M3.1-Flash")),
    _standard(LOW, HIGH) + "\n" + _standard(LOW, HIGH),
])
def test_missing_or_ambiguous_standard_tiers_stay_unpriced(page: str) -> None:
    assert "minimax/minimax-m3" not in parser.parse(normalize_parser_input(page))


def test_markdown_tiers_survive_manifest_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "minimax.json"
    path.write_text(json.dumps({"models": [
        {"id": "minimax/minimax-m3", "upstream_id": "MiniMax-M3"},
        {"id": "minimax/minimax-m3.1-flash", "upstream_id": "MiniMax-M3.1-Flash"},
    ]}))
    monkeypatch.setattr(provider, "MANIFEST_PATH", path)
    monkeypatch.setattr(provider, "_DISCOVERED_MANIFEST_ROWS", {
        row["id"]: row for row in json.loads(path.read_text())["models"]
    })
    prices, errors = _coerce_to_model_prices(parser.parse(FLASH + "\n" + _standard(LOW, HIGH)))
    assert prices is not None and not errors
    provider.write_provider_manifest(ProviderPricingResult(slug="minimax", prices=prices, source="api"))
    row = next(row for row in json.loads(path.read_text())["models"] if row["id"] == "minimax/minimax-m3")
    assert [tier["max_prompt_tokens"] for tier in row["price_tiers"]] == [512_000, None]
    assert [tier["cached_input_token_price_per_m"] for tier in row["price_tiers"]] == [64_000, 128_000]


def test_outer_manifest_gate_accepts_only_reviewed_retirement_tombstones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TR_LIFECYCLE_CLOCK_OVERRIDE", "2026-11-03T00:00:00Z")
    path = tmp_path / "minimax.json"
    active = [{"id": "minimax/" + name.lower(), "upstream_id": name}
              for name in ("MiniMax-M3", "MiniMax-M3.1-Flash")]
    retired = [{"id": "minimax/" + name.lower(), "upstream_id": name, "missing_since": "2026-10-19"}
               for name in ("MiniMax-M2.5", "MiniMax-M2.7")]
    path.write_text(json.dumps({"models": active + retired}))
    monkeypatch.setattr(provider, "MANIFEST_PATH", path)
    monkeypatch.setattr(provider, "_DISCOVERED_MANIFEST_ROWS", {row["id"]: row for row in active})
    prices, errors = _coerce_to_model_prices(parser.parse(FLASH + "\n" + _standard(LOW, HIGH)))
    assert prices is not None and not errors
    _, failures = refresh._write_provider_manifests({
        "minimax": ProviderPricingResult(slug="minimax", prices=prices, source="api"),
    })
    assert failures == []
    rows = {row["id"]: row for row in json.loads(path.read_text())["models"]}
    assert len(rows) == 4
    assert all(rows[row["id"]]["routable"] is False for row in retired)
    assert all(rows[row["id"]]["routable_reason"] == "delisted-upstream" for row in retired)
    assert all(rows[row["id"]].get("routable") is not False for row in active)
    assert rows["minimax/minimax-m3"]["price_tiers"][0]["max_prompt_tokens"] == 512_000


@pytest.mark.parametrize("mutation", [
    "active-model", "before-retirement", "changed-upstream", "changed-first-miss",
    "missing-first-miss", "operator-hold", "still-discovered", "stale-source",
    "not-a-tombstone", "not-retained",
])
def test_retirement_confirmation_refuses_unreviewed_changes(
    monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    monkeypatch.setenv("TR_LIFECYCLE_CLOCK_OVERRIDE", "2026-11-03T00:00:00Z")
    before = {"id": "minimax/minimax-m2.7", "upstream_id": "MiniMax-M2.7", "missing_since": "2026-10-19"}
    after = {**before, "routable": False, "routable_reason": "delisted-upstream"}
    monkeypatch.setattr(provider, "_DISCOVERED_MANIFEST_ROWS", {})
    result = ProviderPricingResult(slug="minimax", prices={}, source="api")
    if mutation == "active-model":
        before.update(id="minimax/minimax-m3", upstream_id="MiniMax-M3")
        after.update(before)
    elif mutation == "before-retirement":
        monkeypatch.setenv("TR_LIFECYCLE_CLOCK_OVERRIDE", "2026-10-18T00:00:00Z")
    elif mutation == "changed-upstream":
        after["upstream_id"] = "MiniMax-M3"
    elif mutation == "changed-first-miss":
        after["missing_since"] = "2026-11-03"
    elif mutation == "missing-first-miss":
        after.pop("missing_since")
    elif mutation == "operator-hold":
        after["routable_reason"] = "manual-hold"
    elif mutation == "still-discovered":
        monkeypatch.setattr(provider, "_DISCOVERED_MANIFEST_ROWS", {before["id"]: before})
    elif mutation == "stale-source":
        result.source = "stale_manifest"
    elif mutation == "not-a-tombstone":
        after["routable"] = True
    assert provider.confirmed_manifest_quarantines(
        result, [before], [] if mutation == "not-retained" else [after],
    ) == frozenset()


def test_reviewed_retirements_do_not_excuse_other_route_losses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TR_LIFECYCLE_CLOCK_OVERRIDE", "2026-11-03T00:00:00Z")
    monkeypatch.setattr(provider, "_DISCOVERED_MANIFEST_ROWS", {})
    before = [
        {"id": "minimax/" + name.lower(), "upstream_id": name, "missing_since": "2026-10-19"}
        for name in ("MiniMax-M2.7", "MiniMax-M3")
    ]
    after = [{**row, "routable": False, "routable_reason": "delisted-upstream"} for row in before]
    confirmed = provider.confirmed_manifest_quarantines(
        ProviderPricingResult(slug="minimax", prices={}, source="api"), before, after,
    )
    assert confirmed == frozenset({"minimax/minimax-m2.7"})
    assert guard_manifest_prune(before, after, confirmed_disabled_ids=confirmed) is before
