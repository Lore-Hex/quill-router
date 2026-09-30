from __future__ import annotations

from bs4 import BeautifulSoup

from trusted_router.content.token_index import CHART_ALT, GRADES, QUOTES


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


def test_token_index_page_publishes_fixed_charts_not_data(client):
    """The page is quotes and fixed chart images: no chart library, no data file,
    no embedded series. Each chart ships a dark and a light rendering."""
    response = client.get("/index")
    page = BeautifulSoup(response.text, "html.parser")
    charts = [img for img in page.select("figure.ti-chart img")]
    assert len(charts) == 4
    for img in charts:
        assert img["alt"] in CHART_ALT.values()
        assert client.get(img["src"]).status_code == 200
    assert client.get("/static/og/index.png").status_code == 200
    assert client.get("/static/token-index.css").status_code == 200
    lowered = response.text.lower()
    for marker in ("plotly", "series.json", "chart.js", "data:application/json"):
        assert marker not in lowered


def test_token_index_quotes_are_whole_dollars_per_billion_tokens():
    assert set(QUOTES) == {"ALL", "AAA", "A", "B", "C"}
    for quote in QUOTES.values():
        assert isinstance(quote["value"], int) and quote["value"] > 0
        assert isinstance(quote["change_7d"], float)
    assert [grade["code"] for grade in GRADES] == ["AAA", "A", "B", "C"]
    # grades are priced by capability: each grade's quote sits above the next one down
    values = [int(QUOTES[str(grade["code"])]["value"]) for grade in GRADES]
    assert values == sorted(values, reverse=True)
