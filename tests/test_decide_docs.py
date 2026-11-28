import re
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from tests.fixture_routes import bypass_catalog_caches, drop_routes, serve_on_fixture_route
from tests.test_decide_models import _serve_on_fixture_routes
from trusted_router import catalog, catalog_data, decision_model_page, provider_lifecycle
from trusted_router.catalog import (
    MODEL_ENDPOINTS,
    MODELS,
    NATIVE_DECISION_MODEL_IDS,
    PROVIDERS,
    endpoints_for_model,
)
from trusted_router.catalog_data import (
    NAMED_DECISION_MODEL_PROVIDERS,
    PRIVATE_PROXY_MODEL_TARGETS,
)

JEV = "typesafe-ai/jev"
TREV = "trustedrouter/trev-1.0"
MEV = "trustedrouter/mev-1.0"
LEV = "trustedrouter/lev-1.0"
ZEV = "trustedrouter/zev-1.0"
GEV = "trustedrouter/gev-1.0"
DEV = "trustedrouter/dev-1.0"
OEV = "trustedrouter/oev-1.0"
GEMMEV = "trustedrouter/gemmev-1.0"
# Each decision model on the page and the hosts it is configured to run on.
CHAINS: dict[str, tuple[str, ...]] = {JEV: ("typesafe",), **NAMED_DECISION_MODEL_PROVIDERS}
READING_ORDER = [JEV, TREV, MEV, LEV, ZEV, GEV, DEV, OEV, GEMMEV]
WORDS = {1: "one", 2: "two", 3: "three", 4: "four"}
# Input and output tokens of one decision, as measured on 2026-11-28.
TOKENS = {
    JEV: (445, 86),
    TREV: (478, 207),
    MEV: (418, 64),
    ZEV: (418, 68),
    LEV: (440, 62),
    GEV: (429, 76),
    DEV: (411, 74),
    OEV: (465, 135),
    GEMMEV: (445, 72),
}
# The fixture routes' prices, in microdollars per million tokens.
FIXTURE_PRICES = (1_000_000, 3_000_000)


def test_decide_docs_publish_the_contract(client: TestClient) -> None:
    response = client.get("/docs/decide")

    assert response.status_code == 200
    text = response.text
    assert "/decide" in text and "POST /v1/evaluate" in text
    for field in ('"state"', '"questions"', '"answers"', '"probabilities"', '"inputTokens"'):
        assert field in text, field
    for question_type in ("boolean", "choice", "score"):
        assert f'<span class="card-label">{question_type}</span>' in text
    assert "trustedrouter/trev-1.0" in text
    assert "typesafe-ai/jev" in text


def test_decide_docs_list_exactly_the_models_the_catalog_advertises(client: TestClient) -> None:
    """The model table's copy is written per model; this keeps it from drifting
    from the lineup the gateway is built to serve. A name whose host has
    dropped it for now stays in the table, marked unavailable."""
    text = client.get("/docs/decide").text
    for model_id in (*NATIVE_DECISION_MODEL_IDS, "typesafe-ai/jev"):
        assert model_id in NATIVE_DECISION_MODEL_IDS or MODELS[model_id].supports_decide
        assert f'<span class="mono">{model_id}</span>' in text, (
            f"{model_id} missing from the docs table"
        )
    assert text.count('<tr><td><span class="mono">') - 4 == len(NATIVE_DECISION_MODEL_IDS) + 1, (
        "the docs table lists a model the catalog does not advertise"
    )


def test_decide_docs_state_the_guarantee_and_its_cost_honestly(client: TestClient) -> None:
    text = client.get("/docs/decide").text
    # The guarantee.
    assert "never a guess" in text
    assert "Hosted models are checked exactly as chat models are" in text
    # What it costs the caller, stated rather than buried.
    assert "Both attempts consumed tokens at the model host and both are billed" in text
    # What the numbers are and are not.
    assert "The eval is small and deliberately easy" in text
    assert "your request also pays for authorization and settlement through the gateway" in text
    assert "hosts vary from run to run" in text
    # Where the state goes: the vendor directly, and nowhere else.
    assert "called at TypeSafe's own API and at no other host" in text
    assert "Vercel AI Gateway" not in text.split("same contract as")[0]
    # A client must be told when retrying would pay twice.
    assert "x-should-retry: false" in text


def test_decide_docs_publish_devs_ordered_host_chain(
    client: TestClient, every_chain_live: None
) -> None:
    hosts = [PROVIDERS[host].name for host in CHAINS[DEV]]
    assert len(hosts) >= 2
    page = _page(client)
    assert f"Fails over from {' to '.join(hosts)}." in _model_rows(page)[DEV][1]
    assert _runs_on(page)[PRIVATE_PROXY_MODEL_TARGETS[DEV]] == ", then ".join(hosts)


def test_decide_docs_are_discoverable(client: TestClient) -> None:
    assert 'href="/docs/decide"' in client.get("/docs").text
    assert "/docs/decide" in client.get("/llms.txt").text
    assert "/docs/decide" in client.get("/docs/llms.txt").text
    assert "/docs/decide" in client.get("/docs/llms-full.txt").text
    assert (
        "<loc>https://trustedrouter.com/docs/decide</loc>" in client.get("/sitemap-core.xml").text
    )


def test_decide_docs_lead_with_the_alpha_path_and_promise_every_alias(client: TestClient) -> None:
    """The route's name is POST /api/alpha/decide. Every other path the gateway
    answers to is an alias, listed once, from the same table the rest of the
    site reads -- so a page cannot keep advertising a path as the default after
    the default moves."""
    from trusted_router.catalog_data import DECIDE_PATH, DECIDE_PATH_ALIASES, decide_url

    assert DECIDE_PATH == "/api/alpha/decide"
    # Beside /v1 on the API host, not under it, whatever the base URL looks like.
    for base in ("https://api.trustedrouter.com/v1", "https://api.trustedrouter.com/v1/"):
        assert decide_url(base) == "https://api.trustedrouter.com/api/alpha/decide"
    assert decide_url("http://localhost:8000") == "http://localhost:8000/api/alpha/decide"

    text = client.get("/docs/decide").text
    assert f"<code>POST {DECIDE_PATH}</code>" in text
    assert "/api/alpha/decide \\" in text, "the curl example calls the default path"
    assert "/v1/decide \\" not in text, "an example still calls the old default"
    assert [path for path, _ in DECIDE_PATH_ALIASES] == [
        "/api/decide",
        "/v1/decide",
        "/v1/evaluate",
        "/api/alpha/decisions",
        "/api/decisions",
    ], "mirrors isDecidePath in the attested gateway; change both together"
    for path, _ in DECIDE_PATH_ALIASES:
        assert f"<code>POST {path}</code>" in text, path
    # Callers coming from OpenRouter or TypeSafe: their spelling, their shape.
    assert "&#34;type&#34;: &#34;noul&#34;" in text or '"type": "noul"' in text
    assert "typesafe/jev-1.13" in text


def test_every_page_that_shows_a_decide_call_uses_the_default_path(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The two names below are offered here on fixture routes, whether or not
    # their hosts serve their backing models today.
    bypass_catalog_caches(monkeypatch)
    for model_id in ("trustedrouter/mev-1.0", "trustedrouter/trev-1.0"):
        _serve_on_fixture_routes(monkeypatch, model_id)
    for path in (
        "/docs",
        "/models/trustedrouter/mev-1.0/api",
        "/compare/models/trustedrouter/mev-1.0/vs/trustedrouter/trev-1.0",
    ):
        page = client.get(path)
        assert page.status_code == 200, (path, page.text[:200])
        assert "/api/alpha/decide" in page.text, path
        assert "/v1/decide" not in page.text, path


def test_no_template_names_the_decide_path_by_hand() -> None:
    """The path moved once already, and a page that spelled it out kept
    advertising the old one. /docs/decide may LIST /v1/decide, and does so from
    DECIDE_PATH_ALIASES; no template writes either path as a literal."""
    from pathlib import Path

    offenders = [
        str(template)
        for template in Path("src/trusted_router/templates").rglob("*.html")
        if any(literal in template.read_text() for literal in ("/v1/decide", "/api/alpha/decide"))
    ]
    assert offenders == []



# What the page says about hosts, latency and cost is read from the catalog
# when it is rendered (trusted_router.decision_model_page). The tests below
# build the chains on fixture routes, so they hold for any provider manifest
# on any date.


def _serve(
    monkeypatch: pytest.MonkeyPatch,
    model_id: str,
    hosts: tuple[str, ...],
    prices: dict[str, tuple[int, int]] | None = None,
) -> None:
    """Make `hosts` the only hosts with a prepaid route for the model behind
    `model_id`. `prices` gives a host its own prompt and completion price."""
    backing = PRIVATE_PROXY_MODEL_TARGETS.get(model_id, model_id)
    drop_routes(monkeypatch, backing)
    for host in hosts:
        prompt, completion = (prices or {}).get(host, FIXTURE_PRICES)
        serve_on_fixture_route(
            monkeypatch,
            backing,
            host,
            author=backing.split("/")[0],
            prompt_price_microdollars_per_million_tokens=prompt,
            completion_price_microdollars_per_million_tokens=completion,
        )


@pytest.fixture
def every_chain_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every decision model on its whole configured chain, on fixture routes,
    with no lifecycle retirement in force. A test then darkens the hosts it is
    about with _serve."""
    bypass_catalog_caches(monkeypatch)
    monkeypatch.setattr(catalog, "provider_model_retired", lambda *args, **kwargs: False)
    for model_id, chain in CHAINS.items():
        _serve(monkeypatch, model_id, chain)


def _page(client: TestClient) -> BeautifulSoup:
    response = client.get("/docs/decide")
    assert response.status_code == 200, response.text[:300]
    return BeautifulSoup(response.text, "html.parser")


def _table_rows(page: BeautifulSoup, first_heading: str) -> list[list[str]]:
    table = next(
        table for table in page.find_all("table") if table.find("th").get_text() == first_heading
    )
    return [[cell.get_text() for cell in row.find_all("td")] for row in table.tbody.find_all("tr")]


def _model_rows(page: BeautifulSoup) -> dict[str, list[str]]:
    """The model table in page order. Each row: id, pick it for, latency,
    accuracy, cost."""
    return {row[0]: row for row in _table_rows(page, "Model")}


def _runs_on(page: BeautifulSoup) -> dict[str, str]:
    return {row[0]: row[1] for row in _table_rows(page, "Tuned chat model")}


def _paragraph(page: BeautifulSoup, opening: str) -> str:
    return next(
        text
        for text in (paragraph.get_text() for paragraph in page.find_all("p"))
        if text.startswith(opening)
    )


def _intro(page: BeautifulSoup) -> str:
    return _paragraph(page, "Every model below")


def _shown_ms(latency: str) -> int | None:
    """The figure a latency cell shows, in milliseconds; None when it shows none."""
    match = re.fullmatch(r"about ([\d.]+) (ms|s)", latency)
    if match is None:
        assert not re.search(r"\d", latency), latency
        return None
    return round(float(match[1]) * (1000 if match[2] == "s" else 1))


def _names(hosts: tuple[str, ...] | list[str]) -> list[str]:
    return [PROVIDERS[host].name for host in hosts]


def _fast_claim(page: BeautifulSoup) -> tuple[int, tuple[str, str] | None]:
    """How many rows show at most 400 ms, and what the opening sentence says."""
    shown = [_shown_ms(row[2]) for row in _model_rows(page).values()]
    fast = sum(1 for ms in shown if ms is not None and ms <= 400)
    said = re.search(r": (\w+) of these (answers?) in about a third of a second\.", _intro(page))
    return fast, ((said[1], said[2]) if said else None)


def _cost(model_id: str, prices: tuple[int, int] = FIXTURE_PRICES) -> str:
    """One decision of `model_id` at these prices, as the cost column writes
    it: the measured tokens at the route's price, to two significant figures."""
    (tokens_in, tokens_out), (prompt, completion) = TOKENS[model_id], prices
    dollars = (tokens_in * prompt + tokens_out * completion) / 1e12
    return "$" + f"{float(f'{dollars:.2g}'):.12f}".rstrip("0")


def _later_hosts() -> list[tuple[str, tuple[str, ...]]]:
    """Each name with a chain of two or more, on every run of hosts that
    starts after its first."""
    return [
        (model_id, chain[start:])
        for model_id, chain in CHAINS.items()
        for start in range(1, len(chain))
    ]


def test_with_every_chain_live_each_row_shows_its_first_hosts_figure(
    client: TestClient, every_chain_live: None
) -> None:
    page = _page(client)
    rows = _model_rows(page)
    # Fastest first; trev and mev show the same figure and keep reading order.
    assert [(model_id, row[2]) for model_id, row in rows.items()] == [
        (JEV, "about 250 ms"),
        (TREV, "about 340 ms"),
        (MEV, "about 340 ms"),
        (DEV, "about 370 ms"),
        (ZEV, "about 410 ms"),
        (GEMMEV, "about 450 ms"),
        (LEV, "about 0.8 s"),
        (GEV, "about 0.9 s"),
        (OEV, "about 1.4 s"),
    ]
    trev_hosts = WORDS[len(CHAINS[TREV])]
    assert rows[TREV][1] == (
        "The default. An open-weight 120B reasoning model on the fastest hardware "
        f"we could measure, with automatic failover across {trev_hosts} hosts."
    )
    assert rows[LEV][1] == (
        "A widely deployed open model. Llama 3.3 70B, answering without deliberating. "
        f"Fails over across {WORDS[len(CHAINS[LEV])]} hosts."
    )
    assert len(CHAINS[ZEV]) == 2
    # The price comparison is computed from the costs (see the price tests);
    # fixture routes share one price, so only its form is pinned here.
    assert rows[ZEV][1].startswith(
        "A larger fast model. GLM 5.3 Fast, which reasons briefly, with failover to a "
        "second host. About "
    ) and rows[ZEV][1].endswith("trev's price.")
    assert rows[GEMMEV][1] == (
        "An open Gemma model: Gemma 4 26B with 4B parameters active, answering without "
        f"deliberating. Fails over across {WORDS[len(CHAINS[GEMMEV])]} hosts."
    )
    # Single-host names say nothing about hosts.
    assert rows[MEV][1].endswith("Mercury 3, a diffusion model, answering without deliberating.")
    assert _intro(page).endswith(
        "so speed comes first: four of these answer in about a third of a second. "
        "Start with trustedrouter/trev-1.0: it is among the fastest, it reasons briefly "
        f"before it answers, and it fails over across {trev_hosts} hosts."
    )
    assert f'"fallback_candidate_count": {len(CHAINS[TREV])},' in page.get_text()


def test_every_latency_figure_is_one_measurement_described_once(
    client: TestClient, every_chain_live: None
) -> None:
    """One method for every figure: the pinned host's own median time on the
    day of the eval, the gateway's time left out. No row carries a figure taken
    another way, and the page publishes no figure for the gateway's time."""
    page = _page(client)
    how = _paragraph(page, "How to read this.")
    assert "29 checks, run on 28 November 2026." in how
    assert (
        "Latency is the median time the host took to answer, from the gateway's "
        "records, with the host pinned; hosts vary from run to run. It excludes the "
        "gateway's own time: your request also pays for authorization and settlement "
        "through the gateway. Each model's figure is for its first host."
    ) in how
    # Jev's path records no host time, and the page says where its figure is from.
    assert "Jev's figure is the exception" in how
    text = page.get_text()
    for retired_wording in ("end to end", "like gemmev", "Gemmev's figure", "565 ms"):
        assert retired_wording not in text, retired_wording
    # No latency cell carries a range any more.
    assert not re.search(r"\(\d[\d.]* to \d[\d.]* s\)", text)
    for row in _model_rows(page).values():
        assert "Measured" not in row[1], row
    # Around the words authorization and settlement, no number.
    for sentence in re.split(r"(?<=[.:;]) ", how):
        if "authorization" in sentence or "settlement" in sentence:
            assert not re.search(r"\d", sentence), sentence


def test_the_published_figures_are_first_hosts_over_enough_requests() -> None:
    measured = decision_model_page.MEASURED_LATENCY
    assert decision_model_page.MIN_SAMPLES == 6
    assert {
        model_id: {host: (median.ms, median.samples) for host, median in hosts.items()}
        for model_id, hosts in measured.items()
    } == {
        TREV: {"cerebras": (335, 8)},
        MEV: {"inception": (344, 7)},
        ZEV: {"fireworks": (410, 10)},
        LEV: {"wandb": (805, 13)},
        GEV: {"google-ai-studio": (864, 6)},
        DEV: {"wafer": (371, 10)},
        OEV: {"deepinfra": (1417, 13)},
        GEMMEV: {"nextbit": (450, 11)},
    }
    for model_id, hosts in measured.items():
        assert set(hosts) <= set(CHAINS[model_id]), model_id
    assert decision_model_page.DECISION_TOKENS == TOKENS


@pytest.mark.parametrize(("model_id", "live"), _later_hosts())
def test_a_dark_first_host_leaves_the_row_without_a_figure(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    every_chain_live: None,
    model_id: str,
    live: tuple[str, ...],
) -> None:
    chain = CHAINS[model_id]
    # Its own price at every host, so the cost shown names the host it is for.
    prices = {host: (1_000_000 * rank, 2_000_000 * rank) for rank, host in enumerate(chain, 1)}

    _serve(monkeypatch, model_id, live, prices)
    rows = _model_rows(_page(client))
    row = rows[model_id]

    # The only figures are first hosts'. A later host has none to show, and
    # the row does not keep the first host's.
    where = "its current host" if model_id == TREV else PROVIDERS[live[0]].name
    assert row[2] == f"not yet measured on {where}"
    assert _shown_ms(row[2]) is None
    assert list(rows)[-1] == model_id
    assert row[4] == _cost(model_id, prices[live[0]])
    # Claims that were the first host's go with it.
    for claim in ("steadiest tail", "fastest hardware", "Measured"):
        assert claim not in row[1], claim
    if model_id == TREV:
        # trev's hosts are counted, never named.
        assert not [name for name in _names(chain) if name in " ".join(row)]
    else:
        assert PROVIDERS[chain[0]].name not in row[1]
    if len(live) >= 2 and model_id != DEV:
        assert f"across {WORDS[len(live)]} hosts" in row[1]


@pytest.mark.parametrize(
    ("model_id", "sentence"),
    [
        (LEV, " Served by {host} alone."),
        (DEV, " Served by {host} alone."),
        (GEMMEV, " Served by {host} alone."),
        (ZEV, ", served by {host} alone."),
        (TREV, ", served by a single host."),
    ],
)
def test_a_name_on_one_host_claims_no_failover(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    every_chain_live: None,
    model_id: str,
    sentence: str,
) -> None:
    for host in CHAINS[model_id]:
        _serve(monkeypatch, model_id, (host,))
        page = _page(client)
        row = _model_rows(page)[model_id]
        assert sentence.format(host=PROVIDERS[host].name) in row[1], host
        assert "ails over" not in row[1] and "failover" not in row[1], host
        if model_id == TREV:
            assert _intro(page).endswith("and it is served by a single host.")
            assert "fails over" not in _intro(page)
            assert '"fallback_candidate_count": 1,' in page.get_text()


def test_a_figure_over_too_few_requests_is_not_published(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, every_chain_live: None
) -> None:
    measured = decision_model_page.MEASURED_LATENCY
    second = CHAINS[LEV][1]
    _serve(monkeypatch, LEV, CHAINS[LEV][1:])

    for samples, shown in ((5, f"not yet measured on {PROVIDERS[second].name}"), (6, "about 0.9 s")):
        monkeypatch.setitem(measured[LEV], second, decision_model_page.HostMedian(947, samples))
        assert _model_rows(_page(client))[LEV][2] == shown, samples

    # The same for a first host, and for a name whose hosts the page does not
    # name.
    first = CHAINS[TREV][0]
    monkeypatch.setitem(measured[TREV], first, replace(measured[TREV][first], samples=5))
    assert _model_rows(_page(client))[TREV][2] == "not yet measured on its current host"


@pytest.mark.parametrize("model_id", READING_ORDER)
def test_a_name_with_no_live_host_is_marked_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, every_chain_live: None, model_id: str
) -> None:
    _serve(monkeypatch, model_id, ())
    page = _page(client)
    rows = _model_rows(page)

    assert rows[model_id][1].endswith(" Temporarily unavailable.")
    assert rows[model_id][2] == "unavailable"
    assert rows[model_id][4] == "unavailable"
    assert list(rows)[-1] == model_id
    for claim in ("ails over", "failover", "erved by", "steadiest tail", "fastest hardware"):
        assert claim not in rows[model_id][1], claim
    backing = PRIVATE_PROXY_MODEL_TARGETS.get(model_id, model_id)
    if backing in _runs_on(page):
        assert _runs_on(page)[backing] == "None at present"
    if model_id == TREV:
        assert "Start with" not in _intro(page)
        # The sample response is a served request: it had a candidate.
        assert '"fallback_candidate_count": 1,' in page.get_text()


def test_the_page_renders_with_no_decision_model_live(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, every_chain_live: None
) -> None:
    for model_id in CHAINS:
        _serve(monkeypatch, model_id, ())
    page = _page(client)
    rows = _model_rows(page)

    assert list(rows) == READING_ORDER
    assert {(row[2], row[4]) for row in rows.values()} == {("unavailable", "unavailable")}
    assert set(_runs_on(page).values()) == {"None at present"}
    assert _intro(page).endswith("so speed comes first.")
    assert '"fallback_candidate_count": 1,' in page.get_text()


def test_the_third_of_a_second_count_follows_the_rows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, every_chain_live: None
) -> None:
    assert _fast_claim(_page(client)) == (4, ("four", "answer"))
    _serve(monkeypatch, DEV, CHAINS[DEV][1:])
    assert _fast_claim(_page(client)) == (3, ("three", "answer"))
    _serve(monkeypatch, TREV, CHAINS[TREV][1:])
    page = _page(client)
    assert _fast_claim(page) == (2, ("two", "answer"))
    # trev is recommended as among the fastest only while it is counted here.
    assert "among the fastest" not in _intro(page)
    assert "Start with trustedrouter/trev-1.0: it reasons briefly" in _intro(page)
    _serve(monkeypatch, MEV, ())
    assert _fast_claim(_page(client)) == (1, ("one", "answers"))
    _serve(monkeypatch, JEV, ())
    assert _fast_claim(_page(client)) == (0, None)


def test_levs_row_makes_no_tail_claim_on_any_host(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, every_chain_live: None
) -> None:
    # "The steadiest tail" was SambaNova's. On 2026-11-28 zev's tail was
    # tighter than lev's on W&B, so no host may bring the claim back.
    chain = CHAINS[LEV]
    for live in (chain, chain[:1], (chain[0], chain[-1]), chain[1:], chain[2:], ()):
        _serve(monkeypatch, LEV, live)
        row = _model_rows(_page(client))[LEV]
        assert "steadiest" not in row[1] and "tail" not in row[1], live
        assert row[1].startswith(
            "A widely deployed open model. Llama 3.3 70B, answering without deliberating."
        ), live


def test_the_table_is_ordered_by_the_latency_shown(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, every_chain_live: None
) -> None:
    _serve(monkeypatch, LEV, CHAINS[LEV][1:])
    _serve(monkeypatch, ZEV, CHAINS[ZEV][1:])
    _serve(monkeypatch, MEV, ())
    rows = _model_rows(_page(client))

    # mev, lev and zev show no figure and close the table in reading order.
    assert list(rows) == [JEV, TREV, DEV, GEMMEV, GEV, OEV, MEV, LEV, ZEV]
    shown = [_shown_ms(row[2]) for row in rows.values()]
    figures = [ms for ms in shown if ms is not None]
    assert figures == sorted(figures) and shown == [*figures, None, None, None]


def test_runs_on_lists_each_tuned_models_live_chain_in_order(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, every_chain_live: None
) -> None:
    tuned = {
        PRIVATE_PROXY_MODEL_TARGETS[model_id]: model_id
        for model_id in NAMED_DECISION_MODEL_PROVIDERS
        if PRIVATE_PROXY_MODEL_TARGETS[model_id] in NATIVE_DECISION_MODEL_IDS
    }
    assert set(tuned) == set(NATIVE_DECISION_MODEL_IDS) - set(NAMED_DECISION_MODEL_PROVIDERS)

    assert _runs_on(_page(client)) == {
        backing: ", then ".join(_names(CHAINS[model_id])) for backing, model_id in tuned.items()
    }

    # Each name loses its first host, and the rest are registered in reverse.
    for model_id in tuned.values():
        _serve(monkeypatch, model_id, CHAINS[model_id][:0:-1])
    assert _runs_on(_page(client)) == {
        backing: ", then ".join(_names(CHAINS[model_id][1:])) or "None at present"
        for backing, model_id in tuned.items()
    }


def test_live_hosts_are_the_chain_hosts_with_a_current_prepaid_route(
    monkeypatch: pytest.MonkeyPatch, every_chain_live: None
) -> None:
    first, second, third = CHAINS[DEV]
    backing = PRIVATE_PROXY_MODEL_TARGETS[DEV]
    outsider = next(slug for slug in sorted(PROVIDERS) if slug not in CHAINS[DEV])
    drop_routes(monkeypatch, backing)
    # Registered out of chain order: a host outside the chain, the chain's last
    # host, a chain host with only a bring-your-own-key route, then its first.
    serve_on_fixture_route(monkeypatch, backing, outsider, author="deepseek")
    serve_on_fixture_route(monkeypatch, backing, third, author="deepseek")
    serve_on_fixture_route(monkeypatch, backing, second, author="deepseek", usage_type="BYOK")
    serve_on_fixture_route(monkeypatch, backing, first, author="deepseek")

    found = decision_model_page.decision_model_hosts()

    assert [hosts.model_id for hosts in found] == READING_ORDER
    dev = next(hosts for hosts in found if hosts.model_id == DEV)
    assert dev.live == (first, third)
    assert dev.live_hosts == (PROVIDERS[first].name, PROVIDERS[third].name)
    assert dev.latency == decision_model_page.published_latency(DEV, first)
    assert dev.latency is not None and dev.latency.text == "about 370 ms"


def test_a_retired_route_leaves_the_chain_while_it_is_still_registered(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, every_chain_live: None
) -> None:
    chain = CHAINS[LEV]
    backing = PRIVATE_PROXY_MODEL_TARGETS[LEV]
    monkeypatch.setattr(
        catalog,
        "provider_model_retired",
        lambda provider, model_id, *args, **kwargs: (provider, model_id) == (chain[0], backing),
    )
    page = _page(client)

    assert _runs_on(page)[backing] == ", then ".join(_names(chain[1:]))
    row = _model_rows(page)[LEV]
    assert "steadiest tail" not in row[1] and _shown_ms(row[2]) is None
    assert f"Fails over across {WORDS[len(chain) - 1]} hosts." in row[1]


def test_a_route_past_its_catalog_deadline_leaves_the_chain(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, every_chain_live: None
) -> None:
    """A provider manifest that passed its deadline is no longer current: its
    routes stay registered, and the page stops counting them."""
    chain = CHAINS[LEV]
    backing = PRIVATE_PROXY_MODEL_TARGETS[LEV]
    now = catalog_data._utc_now()
    route_id = f"{backing}@{chain[0]}/prepaid"

    monkeypatch.setitem(
        MODEL_ENDPOINTS,
        route_id,
        replace(MODEL_ENDPOINTS[route_id], catalog_valid_until=now + timedelta(seconds=1)),
    )
    assert _runs_on(_page(client))[backing] == ", then ".join(_names(chain))

    # The deadline itself is already past it.
    monkeypatch.setitem(
        MODEL_ENDPOINTS, route_id, replace(MODEL_ENDPOINTS[route_id], catalog_valid_until=now)
    )
    page = _page(client)
    assert _runs_on(page)[backing] == ", then ".join(_names(chain[1:]))
    row = _model_rows(page)[LEV]
    assert "steadiest tail" not in row[1]
    assert row[2] == f"not yet measured on {PROVIDERS[chain[1]].name}"


def test_cost_is_the_measured_tokens_at_the_first_live_hosts_price(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, every_chain_live: None
) -> None:
    # Every model at the same price: the costs differ by their tokens alone.
    rows = _model_rows(_page(client))
    assert {model_id: row[4] for model_id, row in rows.items()} == {
        model_id: _cost(model_id) for model_id in READING_ORDER
    }
    assert rows[TREV][4] == "$0.0011" and rows[OEV][4] == "$0.00087"

    # The first host is neither the dearest nor the cheapest of its chain.
    first, second, third = CHAINS[DEV]
    prices = {
        first: (2_000_000, 5_000_000),
        second: (9_000_000, 9_000_000),
        third: (500_000, 1_000_000),
    }
    for live, host in (
        ((first, second, third), first),
        ((third, second, first), first),
        ((second, third), second),
        ((third,), third),
    ):
        _serve(monkeypatch, DEV, live, prices)
        assert _model_rows(_page(client))[DEV][4] == _cost(DEV, prices[host]), live
    assert _cost(DEV, prices[first]) == "$0.0012"
    assert _cost(DEV, prices[second]) == "$0.0044"
    assert _cost(DEV, prices[third]) == "$0.00028"


def test_cost_reads_the_price_tier_the_request_is_billed_at(
    monkeypatch: pytest.MonkeyPatch, every_chain_live: None
) -> None:
    from trusted_router.pricing import PriceTier

    backing = PRIVATE_PROXY_MODEL_TARGETS[GEV]
    route_id = f"{backing}@{CHAINS[GEV][0]}/prepaid"
    small = PriceTier(
        max_prompt_tokens=10_000,
        prompt_price_microdollars_per_million_tokens=4_000_000,
        completion_price_microdollars_per_million_tokens=8_000_000,
    )
    large = replace(
        small,
        max_prompt_tokens=None,
        prompt_price_microdollars_per_million_tokens=40_000_000,
        completion_price_microdollars_per_million_tokens=80_000_000,
    )
    monkeypatch.setitem(
        MODEL_ENDPOINTS, route_id, replace(MODEL_ENDPOINTS[route_id], price_tiers=(small, large))
    )

    rows = decision_model_page.decide_page_context()["decide_models"]["rows"]
    gev = next(row for row in rows if row.model_id == GEV)

    assert gev.cost == _cost(GEV, (4_000_000, 8_000_000))


def test_a_route_with_no_token_price_shows_no_computed_cost(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, every_chain_live: None
) -> None:
    free = {host: (0, 0) for chain in CHAINS.values() for host in chain}
    _serve(monkeypatch, JEV, CHAINS[JEV], free)
    _serve(monkeypatch, OEV, CHAINS[OEV], free)
    rows = _model_rows(_page(client))

    # Jev keeps the cost typed for it; no other row has one to fall back on.
    assert rows[JEV][4] == "$0.00002"
    assert rows[OEV][4] == "not stated"


@pytest.mark.parametrize(
    ("dollars", "shown"),
    [
        ("0.0003403", "$0.00034"),
        ("0.0000677", "$0.000068"),
        ("0.0013996", "$0.0014"),
        ("0.0000197", "$0.00002"),
        ("0.000065", "$0.000065"),
        ("0.0000345", "$0.000035"),
        ("0.0000995", "$0.0001"),
        ("0.00125", "$0.0013"),
        ("1.25", "$1.3"),
        ("12.3", "$12"),
        ("123", "$120"),
    ],
)
def test_costs_are_written_to_two_significant_figures(dollars: str, shown: str) -> None:
    assert decision_model_page._two_figures(Decimal(dollars)) == shown


def test_the_billing_card_says_which_host_the_table_cost_is_for(client: TestClient) -> None:
    billing = _paragraph(_page(client), "typesafe-ai/jev bills input tokens only")
    assert billing.endswith(
        "A trustedrouter/ name lists, for input and for output separately, the highest "
        "rate any host in its chain charges. The cost in "
        "the table above is for the first host serving it; a request served by a "
        "fallback host may cost more or less."
    )


@pytest.mark.parametrize("past_every_scheduled_cutover", (False, True))
def test_the_page_names_only_hosts_that_serve_each_model_at_this_clock(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, past_every_scheduled_cutover: bool
) -> None:
    """Over the catalog as it is checked in, at this run's clock and one day
    past the last scheduled cutover: every host, host count, figure and cost
    the page gives for a model belongs to the hosts that can serve it then. A
    retirement that lands without the page following it fails here."""
    bypass_catalog_caches(monkeypatch)
    if past_every_scheduled_cutover:
        day_after = provider_lifecycle.latest_scheduled_cutover() + timedelta(days=1)
        monkeypatch.setenv(
            provider_lifecycle.LIFECYCLE_CLOCK_OVERRIDE_ENV,
            day_after.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    live: dict[str, list[str]] = {}
    first_route = {}
    for model_id, chain in CHAINS.items():
        prepaid = {}
        for endpoint in endpoints_for_model(PRIVATE_PROXY_MODEL_TARGETS.get(model_id, model_id)):
            if endpoint.usage_type == "Credits":
                prepaid.setdefault(endpoint.provider, endpoint)
        live[model_id] = [host for host in chain if host in prepaid]
        first_route[model_id] = prepaid[live[model_id][0]] if live[model_id] else None

    page = _page(client)
    rows = _model_rows(page)
    runs_on = _runs_on(page)

    assert sorted(rows) == sorted(CHAINS)
    for model_id, chain in CHAINS.items():
        hosts = live[model_id]
        text, latency, cost = rows[model_id][1], rows[model_id][2], rows[model_id][4]
        if model_id in NAMED_DECISION_MODEL_PROVIDERS:
            named_dark = [
                host
                for host in chain
                if host not in hosts and PROVIDERS[host].name in f"{text} {latency}"
            ]
            assert not named_dark, (model_id, text)
        counted = re.search(r"across (\w+) hosts", text)
        if counted:
            assert counted[1] == WORDS[len(hosts)], (model_id, text)
        if "second host" in text:
            assert len(hosts) == 2, (model_id, text)
        if "alone" in text or "single host" in text:
            assert len(hosts) == 1, (model_id, text)
        assert (latency == "unavailable") is (not hosts), model_id
        assert (cost == "unavailable") is (not hosts), model_id
        assert text.endswith(" Temporarily unavailable.") is (not hosts), model_id
        # A figure is the first live host's own, or the row shows none.
        figure = decision_model_page.published_latency(model_id, hosts[0]) if hosts else None
        if figure is not None:
            assert latency == figure.text, model_id
        else:
            assert _shown_ms(latency) is None, model_id
        # The cost is the first live host's, at its catalog price now.
        route = first_route[model_id]
        if route is not None:
            dollars = decision_model_page.decision_cost(model_id, route)
            assert dollars is not None, model_id
            assert cost == decision_model_page._two_figures(dollars), model_id
        backing = PRIVATE_PROXY_MODEL_TARGETS.get(model_id, model_id)
        if backing in runs_on:
            assert runs_on[backing] == (", then ".join(_names(hosts)) or "None at present")
    assert "steadiest tail" not in rows[LEV][1]
    assert ("fastest hardware" in rows[TREV][1]) is (live[TREV][:1] == list(CHAINS[TREV][:1]))

    shown = [_shown_ms(row[2]) for row in rows.values()]
    figures_shown = [ms for ms in shown if ms is not None]
    assert figures_shown == sorted(figures_shown)
    assert shown == figures_shown + [None] * (len(shown) - len(figures_shown))
    fast, said = _fast_claim(page)
    assert said == ((WORDS.get(fast, ""), "answers" if fast == 1 else "answer") if fast else None)

    intro = _intro(page)
    trev_hosts = len(live[TREV])
    assert ("Start with" in intro) is (trev_hosts > 0)
    assert (f"it fails over across {WORDS.get(trev_hosts)} hosts." in intro) is (trev_hosts >= 2)
    assert f'"fallback_candidate_count": {max(1, trev_hosts)},' in page.get_text()



@pytest.mark.parametrize(
    ("cost", "trev_cost", "words"),
    [
        ("0.0014", "0.00034", "about four times trev's price"),
        ("0.000068", "0.00034", "about a fifth of trev's price"),
        ("0.00017", "0.00034", "about half of trev's price"),
        ("0.00034", "0.00034", "about trev's price"),
        ("0.0014", "0.00021", "about seven times trev's price"),
        # Within 10% is "trev's price"; beyond that and short of two times or
        # half there is no close plain word, so nothing is said.
        ("1.1", "1", "about trev's price"),
        ("0.9", "1", "about trev's price"),
        ("1.11", "1", None),
        ("1.49", "1", None),
        ("1.5", "1", "about two times trev's price"),
        ("0.89", "1", None),
        ("0.67", "1", None),
        ("0.66", "1", "about half of trev's price"),
        ("10.49", "1", "about ten times trev's price"),
        ("10.5", "1", None),
        ("0.0955", "1", "about a tenth of trev's price"),
        ("0.0950", "1", None),
        ("0.0014", "0.0001", None),
        ("0.00002", "0.00034", None),
        (None, "0.00034", None),
        ("0.0014", None, None),
    ],
)
def test_price_comparisons_follow_the_costs(cost: str | None, trev_cost: str | None, words: str | None) -> None:
    assert decision_model_page.trev_price_comparison(
        None if cost is None else Decimal(cost), None if trev_cost is None else Decimal(trev_cost)
    ) == words


def test_the_rows_compare_with_the_trev_cost_the_table_shows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, every_chain_live: None
) -> None:
    # trev falls back to a host at a quarter of its first host's price, and
    # mev costs more than trev there: the comparisons move with trev's cost.
    first, fallback = CHAINS[TREV][0], CHAINS[TREV][1]
    _serve(monkeypatch, TREV, CHAINS[TREV], {first: (4_000_000, 12_000_000), fallback: (1_000_000, 3_000_000)})
    before = {hosts.model_id: hosts.cost for hosts in decision_model_page.decision_model_hosts()}
    _serve(monkeypatch, TREV, CHAINS[TREV][1:], {fallback: (1_000_000, 3_000_000)})
    _serve(monkeypatch, MEV, CHAINS[MEV], {CHAINS[MEV][0]: (9_000_000, 30_000_000)})
    rows = _model_rows(_page(client))
    costs = {hosts.model_id: hosts.cost for hosts in decision_model_page.decision_model_hosts()}
    assert costs[TREV] is not None and before[TREV] is not None and costs[TREV] < before[TREV]
    assert costs[MEV] is not None and costs[MEV] > costs[TREV]
    # mev pricier than trev: no "speed at N times" claim.
    assert rows[MEV][1].startswith("Speed. ")
    for model in (ZEV, MEV):
        words = decision_model_page.trev_price_comparison(costs[model], costs[TREV])
        if model == ZEV:
            assert rows[ZEV][1].endswith(f" {words[0].upper()}{words[1:]}." if words else "reasons briefly, with failover to a second host.")
        else:
            assert rows[MEV][1].startswith(f"Speed at {words}." if words and "times" not in words else "Speed.")
    # No trev host: no comparison is made.
    _serve(monkeypatch, TREV, ())
    rows = _model_rows(_page(client))
    assert "trev" not in rows[ZEV][1] and "trev" not in rows[MEV][1]



def test_todays_catalog_words_the_comparisons_as_measured(client: TestClient) -> None:
    # On the real catalog the comparisons say what the cost column shows today
    # (zev about four times trev, mev about a fifth), unless prices have moved.
    rows = _model_rows(_page(client))
    costs = {hosts.model_id: hosts.cost for hosts in decision_model_page.decision_model_hosts()}
    for model in (ZEV, MEV):
        words = decision_model_page.trev_price_comparison(costs.get(model), costs.get(TREV))
        if words is not None:
            assert words in rows[model][1].lower(), model
