from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from scripts.pricing.parsers import xiaomi
from scripts.pricing.providers import digitalocean, recraft

FIXTURES = Path(__file__).parent / "fixtures/pricing"


def test_xiaomi_shared_prices_use_realtime_usd_only():
    html = (FIXTURES / "xiaomi_shared_prices.html").read_text()
    prices = xiaomi.parse(html)
    assert len(prices) == 5
    assert prices["xiaomi/mimo-v2.6-pro"] == prices["xiaomi/mimo-v2.5-pro"] == {
        "prompt_micro_per_m": 435_000, "completion_micro_per_m": 870_000,
        "prompt_cached_micro_per_m": 3_600,
    }
    assert prices["xiaomi/mimo-v2.5"] == prices["xiaomi/mimo-v2.6-flash"]
    assert prices["xiaomi/mimo-v2.6-flash"]["completion_micro_per_m"] == 280_000
    assert prices["xiaomi/mimo-v2.6-pro-ultraspeed"]["prompt_micro_per_m"] == 4_350_000


@pytest.mark.parametrize("before,after", [
    ('rowspan="5"', 'rowspan="4"'), ('rowspan="5"', 'rowspan="6"'),
    ('rowspan="2"', 'rowspan="1"'), ('rowspan="2"', 'rowspan="0"'),
    ('rowspan="2"', 'rowspan="NaN"'), ('rowspan="2"', 'rowspan="999999"'),
    ('rowspan="2"', 'colspan="2"'), ('Batch API', 'Priority API'),
])
def test_xiaomi_rejects_ambiguous_shared_prices(before, after):
    html = (FIXTURES / "xiaomi_shared_prices.html").read_text()
    with pytest.raises(ValueError):
        xiaomi.parse(html.replace(before, after))


def test_recraft_uses_raster_column_and_exact_pro_variants():
    html = (FIXTURES / "recraft_raster_columns.html").read_text()
    assert recraft._parse_pricing(html) == {
        "recraft/recraftv4_1": 35_000, "recraft/recraftv4_1_utility": 35_000,
        "recraft/recraftv4_1_pro": 210_000, "recraft/recraftv4_1_utility_pro": 210_000,
        "recraft/recraftv4": 40_000, "recraft/recraftv4_pro": 250_000,
        "recraft/recraftv3": 40_000, "recraft/recraftv2": 22_000,
    }
    soup = BeautifulSoup(html, "html.parser")
    for row in soup.select("table")[0].select("tr"):
        cells = row.find_all(["td", "th"])
        row.append(cells[1].extract())
    assert recraft._parse_pricing(str(soup)) == recraft._parse_pricing(html)


@pytest.mark.parametrize("before,after", [
    ("$0.035", "EUR 0.035"), ("$0.035", "$0"), ("$0.035", "$NaN"),
    ("$0.21", "-"), ("<th>Vector</th>", "<th>Raster</th>"),
])
def test_recraft_rejects_missing_ambiguous_or_non_usd_prices(before, after):
    html = (FIXTURES / "recraft_raster_columns.html").read_text()
    with pytest.raises(RuntimeError):
        recraft._parse_pricing(html.replace(before, after))


def test_recraft_conflicting_duplicate_prices_are_rejected():
    html = (FIXTURES / "recraft_raster_columns.html").read_text()
    with pytest.raises(RuntimeError, match="conflicting"):
        recraft._parse_pricing(html + html.replace("$0.035", "$0.05"))


DO_ROW = "| Z.ai | [GLM-5.2](https://huggingface.co/zai-org/GLM-5.2) | USD per 1 million tokens. Standard, All prompts: Input $1.40; Output $4.40; Cache read $0.21. |"


def test_digitalocean_labeled_standard_prices_and_cache():
    price = digitalocean._official_prices(DO_ROW)["z-ai/glm-5.2"]
    assert price.prompt_micro_per_m == 1_400_000
    assert price.completion_micro_per_m == 4_400_000
    assert price.tiers[0].prompt_cached_micro_per_m == 210_000
    assert digitalocean._official_prices(DO_ROW.replace("Z.ai | ", "")) == {"z-ai/glm-5.2": price}


@pytest.mark.parametrize("before,after", [
    ("Standard, All prompts", "Fast Mode, All prompts"),
    ("All prompts", "> 200K tokens"), ("$1.40", "$NaN"),
    ("Cache read", "Cache write"), ("Output $4.40", "Output EUR4.40"),
    ("$0.21.", "$0.21. Batch: Input $0.70; Output $2.20."),
])
def test_digitalocean_never_flattens_unsupported_tariffs(before, after):
    with pytest.raises(RuntimeError, match="unsupported tariff"):
        digitalocean._official_prices(DO_ROW.replace(before, after))


def test_digitalocean_rejects_conflicting_duplicate_prices():
    with pytest.raises(RuntimeError, match="conflicting"):
        digitalocean._official_prices(DO_ROW + "\n" + DO_ROW.replace("$4.40", "$5.00"))
