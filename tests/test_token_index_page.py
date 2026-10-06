from __future__ import annotations

from datetime import date

from bs4 import BeautifulSoup

from trusted_router.content.token_index import AS_OF_ISO, CHART_ALT, GRADES, QUOTES


def test_token_index_page_quotes_the_index_and_its_grades(client):
    response = client.get("/index")
    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    assert page.h1 is not None and page.h1.get_text(strip=True) == "NYTE Token Index"
    text = page.get_text(" ", strip=True)
    assert f"${QUOTES['ALL']['value']:,}" in text
    for grade in GRADES:
        assert f"${QUOTES[str(grade['code'])]['value']:,}" in text
        assert str(grade["name"]) in text
        for model in grade["examples"]:
            assert model in text
    assert page.find("link", rel="canonical")["href"] == "https://trustedrouter.com/index"
    assert "<loc>https://trustedrouter.com/index</loc>" in client.get("/sitemap-core.xml").text


def test_token_index_page_draws_interactive_charts_over_fixed_images(client):
    """Each chart ships a dark and a light image (the no-JS fallback and the link-preview look) in a
    figure that token-index.js turns into an interactive chart drawn from the daily closes. No
    third-party chart library, nothing embedded in the page."""
    response = client.get("/index")
    page = BeautifulSoup(response.text, "html.parser")
    figures = page.select("figure.ti-chart[data-ti-chart]")
    assert [figure["data-ti-chart"] for figure in figures] == ["nyte", "grades"]
    for figure in figures:
        images = figure.select("img")
        assert len(images) == 2
        for img in images:
            assert img["alt"] in CHART_ALT.values()
            assert client.get(img["src"]).status_code == 200
        assert figure["data-series"].startswith("/static/token-index/series.json?v=")
        assert client.get(figure["data-series"]).status_code == 200
    assert any(script["src"].startswith("/static/token-index.js?v=") for script in page.select("script[src]"))
    assert client.get("/static/token-index.js").status_code == 200
    assert client.get("/static/og/index.png").status_code == 200
    assert client.get("/static/token-index.css").status_code == 200
    lowered = response.text.lower()
    for marker in ("plotly", "chart.js", "cdn.jsdelivr.net", "data:application/json"):
        assert marker not in lowered


def test_token_index_series_holds_only_the_daily_closes(client):
    """The charts' data is what they draw and nothing more: one rounded value per series per UTC day
    (whole dollars per billion tokens from $100, one decimal below), ending at the page's quotes."""
    data = client.get("/static/token-index/series.json").json()
    assert set(data) == {"as_of", "unit", "start", "series"}
    assert data["unit"] == "USD per billion tokens"
    assert set(data["series"]) == {"ALL", "AAA", "A", "B", "C"}
    lengths = {len(values) for values in data["series"].values()}
    assert len(lengths) == 1
    start, as_of = date.fromisoformat(data["start"]), date.fromisoformat(data["as_of"])
    assert data["as_of"] == AS_OF_ISO
    assert (as_of - start).days + 1 == lengths.pop()
    for key, values in data["series"].items():
        for value in values:
            assert value is None or (isinstance(value, int) and value >= 100) or (isinstance(value, float) and value < 100 and round(value, 1) == value)
        assert round(values[-1]) == QUOTES[key]["value"]


def test_token_index_quotes_are_whole_dollars_per_billion_tokens():
    assert set(QUOTES) == {"ALL", "AAA", "A", "B", "C"}
    for quote in QUOTES.values():
        assert isinstance(quote["value"], int) and quote["value"] > 0
        assert isinstance(quote["change_7d"], float)
    assert [grade["code"] for grade in GRADES] == ["AAA", "A", "B", "C"]
    # grades are priced by capability: each grade's quote sits above the next one down
    values = [int(QUOTES[str(grade["code"])]["value"]) for grade in GRADES]
    assert values == sorted(values, reverse=True)
