"""A model's publisher is its maker, whichever host serves or delists it.

Model.provider is the default route. For an author without its own route
mapping (catalog_ingest._AUTHOR_TO_PROVIDER_SLUG) it is the first host that
lists the model, so model pages named DeepInfra as the publisher of MiniMax M3,
and Novita, Venice or GMI as the publisher of Qwen models.

A maker with its own provider entry is shown with that entry's name, logo and
provider page. A maker without one is shown by name alone. A model id whose
author prefix is a host's namespace names no maker, and none is shown.

The pages run on fixture models and routes, so these hold whatever hosts list.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from bs4 import BeautifulSoup, Tag
from fastapi.testclient import TestClient

from tests.fixture_routes import serve_on_fixture_route
from trusted_router.catalog import PROVIDERS
from trusted_router.catalog_data import Model
from trusted_router.dashboard import _model_publisher
from trusted_router.provider_branding import provider_logo_url


def _model(model_id: str, default_route: str) -> Model:
    return Model(id=model_id, name=model_id, provider=default_route, context_length=131_072)


@pytest.mark.parametrize(
    ("model_id", "default_route", "name", "slug"),
    [
        # The first host to list the model is a reseller.
        ("qwen/fixture-model", "deepinfra", "Alibaba Cloud Model Studio", "alibaba"),
        # Control: the first host is the maker.
        ("qwen/fixture-model", "alibaba", "Alibaba Cloud Model Studio", "alibaba"),
        ("minimax/fixture-model", "deepinfra", "MiniMax", "minimax"),
        ("MiniMaxAI/fixture-model", "nebius", "MiniMax", "minimax"),
        ("cohere/fixture-model", "azure", "Cohere", "cohere"),
        ("zai-org/fixture-model", "novita", "Z.AI", "zai"),
        ("bytedance/fixture-model", "atlas-cloud", "BytePlus ModelArk", "byteplus"),
        ("google/fixture-model", "venice", "Google AI Studio", "google-ai-studio"),
        ("black-forest-labs/fixture-model", "nscale", "Black Forest Labs", "bfl"),
        ("xiaomimimo/fixture-model", "novita", "Xiaomi MiMo", "xiaomi"),
        ("kwaipilot/fixture-model", "novita", "StreamLake", "streamlake"),
        ("PaddlePaddle/fixture-model", "novita", "Baidu AI Cloud Qianfan", "baidu"),
        ("THUDM/fixture-model", "novita", "Z.AI", "zai"),
        ("FunAudioLLM/fixture-model", "siliconflow", "Alibaba Cloud Model Studio", "alibaba"),
        ("Tongyi-MAI/fixture-model", "siliconflow", "Alibaba Cloud Model Studio", "alibaba"),
        ("Wan-AI/fixture-model", "siliconflow", "Alibaba Cloud Model Studio", "alibaba"),
        ("nv-mistralai/fixture-model", "nvidia-nim", "Mistral", "mistral"),
    ],
)
def test_a_maker_with_its_own_provider_entry_publishes_its_models_on_any_host(
    model_id: str, default_route: str, name: str, slug: str
) -> None:
    publisher = _model_publisher(_model(model_id, default_route))
    assert (publisher.name, publisher.slug) == (name, slug)


@pytest.mark.parametrize(
    ("model_id", "default_route", "name"),
    [
        # Routing sends meta-llama to Cerebras; the provider entry named for Meta
        # is "Meta via OpenRouter", a reseller.
        ("meta-llama/fixture-model", "cerebras", "Meta"),
        ("meta/fixture-model", "meta", "Meta"),
        # NVIDIA NIM hosts many labs' models.
        ("nvidia/fixture-model", "nvidia-nim", "NVIDIA"),
        ("ibm-granite/fixture-model", "deepinfra", "IBM"),
        ("Sao10K/fixture-model", "novita", "Sao10K"),
        # An author with no display name is shown as its id spells it.
        ("Fixture-Lab/fixture-model", "novita", "Fixture-Lab"),
    ],
)
def test_a_maker_without_its_own_provider_entry_is_named_without_one(
    model_id: str, default_route: str, name: str
) -> None:
    publisher = _model_publisher(_model(model_id, default_route))
    assert (publisher.name, publisher.slug) == (name, None)


@pytest.mark.parametrize(
    ("model_id", "default_route"),
    [
        ("lightning-ai/fixture-model", "lightning"),
        ("cerebras/fixture-model", "cerebras"),
        ("fal/fixture-model", "fal"),
        ("phala/fixture-model", "phala"),
        ("stealth/fixture-model", "openrouter"),
    ],
)
def test_a_host_namespace_names_no_publisher(model_id: str, default_route: str) -> None:
    publisher = _model_publisher(_model(model_id, default_route))
    assert (publisher.name, publisher.slug) == (None, None)


def test_every_maker_entry_has_a_provider_page_and_a_logo() -> None:
    # A publisher link or logo must never 404 or fall back to TrustedRouter's.
    from trusted_router.catalog_data import AUTHORS_NAMING_NO_MAKER, MAKER_PROVIDER_BY_AUTHOR

    for author, slug in MAKER_PROVIDER_BY_AUTHOR.items():
        assert author == author.lower(), author
        assert slug in PROVIDERS, author
        assert provider_logo_url(slug) == f"/static/provider-logos/{slug}.png", author
    assert not AUTHORS_NAMING_NO_MAKER & set(MAKER_PROVIDER_BY_AUTHOR)


# --- Pages, on fixture models ------------------------------------------------

QWEN_ON_A_RESELLER = "qwen/fixture-publisher-on-deepinfra"
QWEN_AT_ITS_MAKER = "qwen/fixture-publisher-on-alibaba"
MAKER_WITHOUT_ENTRY = "meta-llama/fixture-publisher-no-entry"
HOST_NAMESPACE = "lightning-ai/fixture-publisher-no-maker"

# Each fixture model is served by one host, which is also its default route.
_FIXTURE_MODELS = (
    (QWEN_ON_A_RESELLER, "Fixture Qwen on DeepInfra", "deepinfra"),
    (QWEN_AT_ITS_MAKER, "Fixture Qwen on Alibaba", "alibaba"),
    (MAKER_WITHOUT_ENTRY, "Fixture Llama on Cerebras", "cerebras"),
    (HOST_NAMESPACE, "Fixture model on Lightning", "lightning"),
)

# The publisher each page shows: (name, provider page link, logo).
ALIBABA = ("Alibaba Cloud Model Studio", "/providers/alibaba", "/static/provider-logos/alibaba.png")
SHOWN_PUBLISHER = {
    QWEN_ON_A_RESELLER: ALIBABA,
    QWEN_AT_ITS_MAKER: ALIBABA,
    MAKER_WITHOUT_ENTRY: ("Meta", None, None),
    HOST_NAMESPACE: ("Not stated", None, None),
}
BRAND = {
    QWEN_ON_A_RESELLER: {
        "@type": "Brand",
        "name": "Alibaba Cloud Model Studio",
        "logo": "https://trustedrouter.com/static/provider-logos/alibaba.png",
    },
    QWEN_AT_ITS_MAKER: {
        "@type": "Brand",
        "name": "Alibaba Cloud Model Studio",
        "logo": "https://trustedrouter.com/static/provider-logos/alibaba.png",
    },
    MAKER_WITHOUT_ENTRY: {"@type": "Brand", "name": "Meta"},
    HOST_NAMESPACE: None,
}


@pytest.fixture
def fixture_models(monkeypatch: pytest.MonkeyPatch) -> None:
    for model_id, name, host in _FIXTURE_MODELS:
        serve_on_fixture_route(
            monkeypatch,
            model_id,
            host,
            author=host,
            model=Model(id=model_id, name=name, provider=host, context_length=131_072),
        )


def _shown(element: Tag) -> tuple[str, str | None, str | None]:
    """The (name, link, logo) a rendered publisher element shows."""
    link = element if element.name == "a" else element.find("a")
    logo = element.find("img")
    name = element.find("strong") or element
    return (
        " ".join(name.get_text(" ", strip=True).split()),
        link.get("href") if isinstance(link, Tag) else None,
        logo.get("src") if isinstance(logo, Tag) else None,
    )


def _soup(client: TestClient, path: str) -> BeautifulSoup:
    response = client.get(path)
    assert response.status_code == 200, path
    return BeautifulSoup(response.text, "html.parser")


def _json_ld(soup: BeautifulSoup) -> dict[str, Any]:
    script = soup.select_one('script[type="application/ld+json"]')
    assert script is not None and script.string is not None
    return {node["@type"]: node for node in json.loads(script.string)["@graph"]}


@pytest.mark.usefixtures("fixture_models")
def test_the_model_page_names_the_maker(client: TestClient) -> None:
    for model_id, shown in SHOWN_PUBLISHER.items():
        soup = _soup(client, f"/models/{model_id}")

        header = soup.find("th", string="Publisher")
        assert header is not None, model_id
        row = header.find_next_sibling("td")
        assert isinstance(row, Tag), model_id
        assert _shown(row) == shown, model_id

        chip = soup.select_one(".panel-head .provider-chip")
        if model_id == HOST_NAMESPACE:
            assert chip is None, model_id
        else:
            assert chip is not None, model_id
            assert _shown(chip) == shown, model_id

        for page in (soup, _soup(client, f"/models/{model_id}/pricing")):
            assert _json_ld(page)["Service"].get("brand") == BRAND[model_id], model_id


@pytest.mark.usefixtures("fixture_models")
def test_the_comparison_page_names_each_maker(client: TestClient) -> None:
    for left, right in (
        (MAKER_WITHOUT_ENTRY, QWEN_ON_A_RESELLER),
        (HOST_NAMESPACE, QWEN_AT_ITS_MAKER),
    ):
        soup = _soup(client, f"/compare/models/{left}/vs/{right}")
        row = next(
            row
            for row in soup.select(".matrix-row")
            if row.span and row.span.get_text() == "Publisher"
        )
        cells = row.find_all("span", recursive=False)[1:]
        assert [_shown(cell) for cell in cells] == [
            SHOWN_PUBLISHER[left],
            SHOWN_PUBLISHER[right],
        ], (left, right)


@pytest.mark.usefixtures("fixture_models")
def test_the_seo_listings_name_each_maker(client: TestClient) -> None:
    for path in ("/rankings", "/benchmarks"):
        soup = _soup(client, path)
        for model_id, shown in SHOWN_PUBLISHER.items():
            code = soup.find("code", string=model_id)
            assert code is not None, (path, model_id)
            row = code.find_parent("tr")
            assert row is not None, (path, model_id)
            assert _shown(row.find_all("td", recursive=False)[1]) == shown, (path, model_id)


@pytest.mark.usefixtures("fixture_models")
def test_the_model_listing_shows_a_publisher_logo_only_for_a_maker_entry(
    client: TestClient,
) -> None:
    soup = _soup(client, "/models")
    for model_id, (_name, link, logo) in SHOWN_PUBLISHER.items():
        card = soup.select_one(f'[data-model-card][data-model-id="{model_id}"]')
        assert card is not None, model_id
        publisher_logo = card.select_one(".model-publisher-logo")
        if link is None:
            assert publisher_logo is None, model_id
        else:
            assert publisher_logo is not None, model_id
            assert _shown(publisher_logo)[1:] == (link, logo), model_id
    # Searching the listing for the publisher it shows finds the model.
    card = soup.select_one(f'[data-model-card][data-model-id="{QWEN_ON_A_RESELLER}"]')
    assert card is not None
    assert "alibaba cloud model studio" in str(card["data-search-text"])


def test_which_providers_serve_a_model_lists_its_hosts_not_its_maker(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each model here is served only by DeepInfra, whatever its maker.
    for model_id in ("minimax/fixture-publisher-faq", "qwen/fixture-publisher-faq"):
        serve_on_fixture_route(
            monkeypatch,
            model_id,
            "deepinfra",
            author="deepinfra",
            model=Model(
                id=model_id, name="Fixture FAQ model", provider="deepinfra", context_length=131_072
            ),
        )
        faq = _json_ld(_soup(client, f"/models/{model_id}"))["FAQPage"]
        answer = next(
            item["acceptedAnswer"]["text"]
            for item in faq["mainEntity"]
            if item["name"] == "Which providers serve Fixture FAQ model?"
        )
        assert answer.startswith(
            "TrustedRouter currently lists DeepInfra for Fixture FAQ model."
        ), (
            model_id,
            answer,
        )
