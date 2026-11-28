import re
from datetime import timedelta

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from tests.fixture_routes import bypass_catalog_caches, drop_routes, serve_on_fixture_route
from tests.test_decide_models import _serve_on_fixture_routes
from trusted_router import catalog, decision_model_page, provider_lifecycle
from trusted_router.catalog import (
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



# What the page says about hosts and latency is read from the catalog when it
# is rendered (trusted_router.decision_model_page). The tests below build the
# chains on fixture routes, so they hold for any provider manifest on any date.


def _serve(monkeypatch: pytest.MonkeyPatch, model_id: str, hosts: tuple[str, ...]) -> None:
    """Make `hosts` the only hosts with a prepaid route for the model behind
    `model_id`."""
    backing = PRIVATE_PROXY_MODEL_TARGETS.get(model_id, model_id)
    drop_routes(monkeypatch, backing)
    for host in hosts:
        serve_on_fixture_route(monkeypatch, backing, host, author=backing.split("/")[0])


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


def _intro(page: BeautifulSoup) -> str:
    return next(
        text
        for text in (paragraph.get_text() for paragraph in page.find_all("p"))
        if text.startswith("Every model below")
    )


def _shown_ms(latency: str) -> int | None:
    """The figure a latency cell shows, in milliseconds; None when it shows none."""
    match = re.match(r"about ([\d.]+) (ms|s)\b", latency)
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


def test_with_every_chain_live_each_row_shows_its_first_hosts_figure(
    client: TestClient, every_chain_live: None
) -> None:
    page = _page(client)
    rows = _model_rows(page)
    assert {model_id: row[2] for model_id, row in rows.items()} == {
        JEV: "about 250 ms",
        TREV: "about 300 ms",
        MEV: "about 370 ms",
        LEV: "about 390 ms",
        ZEV: "about 0.8 s (0.7 to 1.1 s)",
        GEV: "about 0.8 s",
        DEV: "about 1 s (0.7 to 2.4 s)",
        OEV: "about 1.6 s",
        GEMMEV: "about 2 s",
    }
    # Those figures are already fastest first; equal ones keep reading order.
    assert list(rows) == READING_ORDER
    trev_hosts = WORDS[len(CHAINS[TREV])]
    assert rows[TREV][1] == (
        "The default. An open-weight 120B reasoning model on the fastest hardware "
        f"we could measure, with automatic failover across {trev_hosts} hosts."
    )
    assert rows[LEV][1] == (
        "The steadiest tail. Llama 3.3 70B does not deliberate, so its slowest answer "
        f"in the eval took 565 ms. Fails over across {WORDS[len(CHAINS[LEV])]} hosts."
    )
    assert len(CHAINS[ZEV]) == 2
    assert rows[ZEV][1] == (
        "A larger fast model. GLM 5.3 Fast, which reasons briefly, with failover to a "
        "second host. About four times the price of the others here. Measured end to "
        "end through the gateway, like gemmev."
    )
    assert rows[GEMMEV][1] == (
        "An open Gemma model: Gemma 4 26B with 4B parameters active, answering without "
        f"deliberating. Fails over across {WORDS[len(CHAINS[GEMMEV])]} hosts."
    )
    # Single-host names say nothing about hosts.
    assert rows[MEV][1] == (
        "Speed at a fifth of the price. Mercury 3, a diffusion model, answering "
        "without deliberating."
    )
    assert _intro(page).endswith(
        "so speed comes first: four of these answer in about a third of a second. "
        "Start with trustedrouter/trev-1.0: it is among the fastest, it reasons briefly "
        f"before it answers, and it fails over across {trev_hosts} hosts."
    )
    assert f'"fallback_candidate_count": {len(CHAINS[TREV])},' in page.get_text()


@pytest.mark.parametrize(
    ("model_id", "live", "latency"),
    [
        (LEV, ("parasail", "together"), "about 0.9 s"),
        (LEV, ("together",), "about 1.6 s"),
        (TREV, ("sambanova", "fireworks", "together"), "about 520 ms"),
        (TREV, ("fireworks", "together"), "about 0.9 s"),
        (TREV, ("together",), "about 1.1 s"),
        (DEV, ("deepinfra", "wandb"), "about 1.9 s"),
        (DEV, ("wandb",), "about 2 s"),
        (GEMMEV, ("wandb", "io-net"), "about 1.9 s"),
        (GEMMEV, ("io-net",), "about 2 s"),
    ],
)
def test_a_dark_first_host_hands_the_row_to_the_next_hosts_figure(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    every_chain_live: None,
    model_id: str,
    live: tuple[str, ...],
    latency: str,
) -> None:
    chain = CHAINS[model_id]
    assert live == chain[-len(live) :], "the chain these figures were measured on has changed"

    _serve(monkeypatch, model_id, live)
    row = _model_rows(_page(client))[model_id]

    assert row[2] == latency
    host = PROVIDERS[live[0]].name
    if model_id == TREV:
        # trev's hosts are counted, never named.
        assert row[1].endswith(" Measured end to end through the gateway, like gemmev.")
        assert not [name for name in _names(chain) if name in row[1]]
    elif model_id == GEMMEV:
        assert row[1].endswith(f" Measured end to end through the gateway on {host}.")
    else:
        assert row[1].endswith(f" Measured end to end through the gateway on {host}, like gemmev.")
    assert PROVIDERS[chain[0]].name not in row[1]
    # Claims that were the first host's go with it.
    assert "565 ms" not in row[1] and "fastest hardware" not in row[1]
    if len(live) >= 2:
        assert f"across {WORDS[len(live)]} hosts" in row[1] or (
            f"Fails over from {' to '.join(_names(live))}." in row[1]
        )


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


def test_an_unmeasured_first_live_host_shows_no_figure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, every_chain_live: None
) -> None:
    measured = decision_model_page.MEASURED_LATENCY
    assert "baseten" in CHAINS[ZEV] and "baseten" not in measured[ZEV]
    _serve(monkeypatch, ZEV, ("baseten",))
    # A name whose hosts the page does not name: the same rule, without the name.
    monkeypatch.delitem(measured[TREV], CHAINS[TREV][1])
    _serve(monkeypatch, TREV, CHAINS[TREV][1:])

    rows = _model_rows(_page(client))

    assert rows[ZEV][2] == f"not yet measured on {PROVIDERS['baseten'].name}"
    assert rows[TREV][2] == "not yet measured on its current host"
    for model_id in (ZEV, TREV):
        assert _shown_ms(rows[model_id][2]) is None
        assert "Measured end to end" not in rows[model_id][1]
    # Rows that show no figure go last, in reading order.
    assert list(rows)[-2:] == [TREV, ZEV]


@pytest.mark.parametrize("model_id", READING_ORDER)
def test_a_name_with_no_live_host_is_marked_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, every_chain_live: None, model_id: str
) -> None:
    _serve(monkeypatch, model_id, ())
    page = _page(client)
    rows = _model_rows(page)

    assert rows[model_id][1].endswith(" Temporarily unavailable.")
    assert rows[model_id][2] == "unavailable"
    assert list(rows)[-1] == model_id
    for claim in ("ails over", "failover", "erved by", "Measured", "565 ms", "fastest hardware"):
        assert claim not in rows[model_id][1], claim
    backing = PRIVATE_PROXY_MODEL_TARGETS.get(model_id, model_id)
    if backing in _runs_on(page):
        assert _runs_on(page)[backing] == "None at present"
    if model_id == TREV:
        assert "Start with" not in _intro(page)


def test_the_page_renders_with_no_decision_model_live(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, every_chain_live: None
) -> None:
    for model_id in CHAINS:
        _serve(monkeypatch, model_id, ())
    page = _page(client)
    rows = _model_rows(page)

    assert list(rows) == READING_ORDER
    assert {row[2] for row in rows.values()} == {"unavailable"}
    assert set(_runs_on(page).values()) == {"None at present"}
    assert _intro(page).endswith("so speed comes first.")


def test_the_third_of_a_second_count_follows_the_rows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, every_chain_live: None
) -> None:
    assert _fast_claim(_page(client)) == (4, ("four", "answer"))
    _serve(monkeypatch, LEV, CHAINS[LEV][1:])
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


def test_levs_slowest_answer_is_shown_only_while_its_first_host_answers_first(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, every_chain_live: None
) -> None:
    chain = CHAINS[LEV]
    assert chain[0] == "sambanova", "the 565 ms answer was measured on SambaNova"
    for live, shown in (
        (chain, True),
        (chain[:1], True),
        ((chain[0], chain[-1]), True),
        (chain[1:], False),
        (chain[2:], False),
        ((), False),
    ):
        _serve(monkeypatch, LEV, live)
        row = _model_rows(_page(client))[LEV]
        assert ("its slowest answer in the eval took 565 ms" in row[1]) is shown, live
        assert row[1].startswith("The steadiest tail. Llama 3.3 70B does not deliberate"), live


def test_the_table_is_ordered_by_the_latency_shown(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, every_chain_live: None
) -> None:
    _serve(monkeypatch, LEV, ("parasail",))
    _serve(monkeypatch, GEMMEV, ("wandb",))
    _serve(monkeypatch, ZEV, ("baseten",))
    _serve(monkeypatch, MEV, ())
    rows = _model_rows(_page(client))

    # lev at 0.9 s moves below gev's 0.8 s; gemmev at 1.9 s stays last of the
    # measured rows. mev and zev show no figure and close the table in
    # reading order.
    assert list(rows) == [JEV, TREV, GEV, LEV, DEV, OEV, GEMMEV, MEV, ZEV]
    shown = [_shown_ms(row[2]) for row in rows.values()]
    figures = [ms for ms in shown if ms is not None]
    assert figures == sorted(figures) and shown == [*figures, None, None]


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
    assert dev.latency == decision_model_page.MEASURED_LATENCY[DEV][first]


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
    assert "565 ms" not in row[1] and row[2] != "about 390 ms"
    assert f"Fails over across {WORDS[len(chain) - 1]} hosts." in row[1]


@pytest.mark.parametrize("past_every_scheduled_cutover", (False, True))
def test_the_page_names_only_hosts_that_serve_each_model_at_this_clock(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, past_every_scheduled_cutover: bool
) -> None:
    """Over the catalog as it is checked in, at this run's clock and one day
    past the last scheduled cutover: every host, host count and figure the page
    gives for a model belongs to the hosts that can serve it then. A retirement
    that lands without the page following it fails here."""
    bypass_catalog_caches(monkeypatch)
    if past_every_scheduled_cutover:
        day_after = provider_lifecycle.latest_scheduled_cutover() + timedelta(days=1)
        monkeypatch.setenv(
            provider_lifecycle.LIFECYCLE_CLOCK_OVERRIDE_ENV,
            day_after.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    live: dict[str, list[str]] = {}
    for model_id, chain in CHAINS.items():
        prepaid = {
            endpoint.provider
            for endpoint in endpoints_for_model(PRIVATE_PROXY_MODEL_TARGETS.get(model_id, model_id))
            if endpoint.usage_type == "Credits"
        }
        live[model_id] = [host for host in chain if host in prepaid]

    page = _page(client)
    rows = _model_rows(page)
    runs_on = _runs_on(page)

    assert sorted(rows) == sorted(CHAINS)
    for model_id, chain in CHAINS.items():
        hosts = live[model_id]
        text, latency = rows[model_id][1], rows[model_id][2]
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
        assert text.endswith(" Temporarily unavailable.") is (not hosts), model_id
        # A figure is the first live host's own, or the row shows none.
        figures = decision_model_page.MEASURED_LATENCY[model_id]
        if hosts and hosts[0] in figures:
            assert latency == figures[hosts[0]].text, model_id
        else:
            assert _shown_ms(latency) is None, model_id
        backing = PRIVATE_PROXY_MODEL_TARGETS.get(model_id, model_id)
        if backing in runs_on:
            assert runs_on[backing] == (", then ".join(_names(hosts)) or "None at present")
    assert ("565 ms" in rows[LEV][1]) is (live[LEV][:1] == list(CHAINS[LEV][:1]))

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
