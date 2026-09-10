"""September 10 pricing/discovery regressions, independent of runtime billing.

Real page captured from https://api-docs.deepseek.com/quick_start/pricing/
on 2026-09-10 at approximately 11:30Z; synthetic table retained for layout tests.
"""

from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from scripts.pricing import base
from scripts.pricing.parsers import deepseek

_FIXTURE = Path(__file__).parent / "fixtures/pricing/deepseek_post_cutover_synthetic.html"
_REAL_FIXTURE = _FIXTURE.with_name("deepseek_pricing_2026-09-10.html")
_FLASH_PRICE = {
    "prompt_micro_per_m": 150_000,
    "completion_micro_per_m": 600_000,
    "prompt_cached_micro_per_m": 3_000,
}
_PRO_PRICE = {
    "prompt_micro_per_m": 660_000,
    "completion_micro_per_m": 1_980_000,
    "prompt_cached_micro_per_m": 22_000,
}
_EXPECTED = {
    "deepseek/deepseek-flash": _FLASH_PRICE,
    "deepseek/deepseek-v4-flash": _FLASH_PRICE,
    "deepseek/deepseek-v4.1-flash": _FLASH_PRICE,
    "deepseek/deepseek-v4-pro": _PRO_PRICE,
}
_DISCOVERED = [
    "deepseek/deepseek-flash",
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4.1-flash",
    "deepseek/deepseek-v4-pro",
]


@pytest.mark.parametrize("fixture", [_REAL_FIXTURE, _FIXTURE], ids=["real", "synthetic"])
@pytest.mark.parametrize("normalized", [False, True], ids=["raw", "normalized"])
def test_post_cutover_fixture_prices_and_discovery(fixture: Path, normalized: bool) -> None:
    html = fixture.read_text(encoding="utf-8")
    if normalized:
        html = base.normalize_parser_input(html)
    parsed = deepseek.parse(html)
    prices, errors = base._coerce_to_model_prices(parsed)
    assert errors == []
    # Drive the exact check that sent run 34467842332 into self-healing. Check
    # discovery first so removing the v4.1 mapping fails with that same message.
    missing_required = sorted(set(_DISCOVERED) - set(parsed))
    assert missing_required == [], f"newly discovered models missing from parser output: {missing_required}"
    assert base.validate(prices, [], required_models=_DISCOVERED) == []
    assert parsed == _EXPECTED
    # The fallback must agree with the captured page for every discovered ID,
    # including when its parseable pricing tables are unavailable.
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        table.decompose()
    assert deepseek.parse(str(soup)) == parsed


@pytest.mark.parametrize("fixture", [_REAL_FIXTURE, _FIXTURE], ids=["real", "synthetic"])
def test_post_cutover_fetch_stays_deterministic(
    fixture: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    html = fixture.read_text(encoding="utf-8")
    monkeypatch.setattr(base, "fetch_html", lambda *_args, **_kwargs: html)

    def unexpected_self_heal(**_kwargs: object) -> str:
        pytest.fail("post-cutover page must not invoke self-healing")

    monkeypatch.setattr(base, "self_heal_parser", unexpected_self_heal)
    result = base.fetch_provider(
        slug="deepseek",
        url="https://api-docs.deepseek.com/quick_start/pricing/",
        expected_models=["deepseek/deepseek-v4-flash"],
        required_models=_DISCOVERED,
        require_runtime_models=False,
    )
    assert result.source == "deterministic"
    assert result.prices == base._coerce_to_model_prices(_EXPECTED)[0]


@pytest.mark.parametrize("pro_first", [False, True], ids=["flash-first", "pro-first"])
def test_pro_owns_its_row_with_september_14_notice(pro_first: bool) -> None:
    soup = BeautifulSoup(_FIXTURE.read_text(encoding="utf-8"), "html.parser")
    if pro_first:
        # Move model names AND their prices together. Assuming a fixed model
        # order would swap Pro and Flash despite the unchanged routing notice.
        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            cells[-2].insert_before(cells[-1].extract())
    assert (
        "From 12:00 Beijing Time on September 14, 2026, and until V4.1 Pro is released "
        "in the future, requests to deepseek-v4-pro will all be routed to V4.1 Flash "
        "and billed at the V4.1 Flash price."
    ) in " ".join(soup.get_text(" ", strip=True).split())
    parsed = deepseek.parse(str(soup))
    assert parsed["deepseek/deepseek-v4-pro"] == _PRO_PRICE
    assert parsed["deepseek/deepseek-flash"] == _FLASH_PRICE


@pytest.mark.parametrize(
    "name", ["deepseek-flash", "deepseek-v4-flash", "deepseek-v4.1-flash", "DeepSeek-V4.1-Flash"]
)
def test_flash_announced_baseline(name: str) -> None:
    assert deepseek.parse(f"<p>Available model: {name}</p>") == {
        f"deepseek/{name.lower()}": _FLASH_PRICE,
    }


def test_pro_announced_baseline_unchanged() -> None:
    assert deepseek.parse("<p>Available model: deepseek-v4-pro</p>") == {
        "deepseek/deepseek-v4-pro": _PRO_PRICE,
    }


@pytest.mark.parametrize("name", ["deepseek-v4.1-flash", "DeepSeek-V4.1-Flash"])
def test_v41_model_header_uses_flash_rolling_price_alias(name: str) -> None:
    html = _FIXTURE.read_text(encoding="utf-8").replace("deepseek-flash", name)
    assert deepseek.parse(html) == _EXPECTED


def test_routing_notice_cannot_supply_inline_pro_prices() -> None:
    html = """
    <p>From 12:00 Beijing Time on September 14, 2026, and until V4.1 Pro is released
    in the future, requests to <code>deepseek-v4-pro</code> will all be routed to
    V4.1 Flash and billed at the V4.1 Flash price.</p>
    <p>deepseek-flash input $0.15 output $0.60</p>
    """
    assert deepseek.parse(html) == {
        "deepseek/deepseek-v4-pro": _PRO_PRICE,
        "deepseek/deepseek-flash": _FLASH_PRICE,
    }


def test_pro_mention_without_a_pro_price_row_is_not_a_flash_alias() -> None:
    html = """
    <table>
      <tr><th>MODEL</th><th>deepseek-flash</th></tr>
      <tr><td>1M INPUT TOKENS (CACHE HIT)</td><td>$0.003</td></tr>
      <tr><td>1M INPUT TOKENS (CACHE MISS)</td><td>$0.15</td></tr>
      <tr><td>1M OUTPUT TOKENS</td><td>$0.60</td></tr>
    </table>
    <p>deepseek-v4-pro will be routed to DeepSeek-V4.1-Flash.</p>
    """
    assert deepseek.parse(html) == {
        "deepseek/deepseek-flash": _FLASH_PRICE,
        "deepseek/deepseek-v4.1-flash": _FLASH_PRICE,
    }
