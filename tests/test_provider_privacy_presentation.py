from dataclasses import replace
from itertools import product

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from trusted_router.catalog import PROVIDERS
from trusted_router.dashboard import _provider_view


@pytest.mark.parametrize("compute,e2ee,zdr", tuple(product((True, False, None), repeat=3)))
def test_confidential_verification_requires_both_provider_flags(
    compute: bool | None, e2ee: bool | None, zdr: bool | None
) -> None:
    provider = replace(
        PROVIDERS["phala"],
        provider_confidential_compute=compute,
        provider_e2ee=e2ee,
        provider_zero_data_retention=zdr,
        attested_gateway=True,
    )
    view = _provider_view(provider)
    verified = compute is True and e2ee is True
    assert view["confidential_inference_verified"] is verified
    assert view["confidential_inference_label"] == (
        "Verified" if verified else "Not verified"
    )
    assert (view["privacy_tier"] == "Confidential") is verified
    assert view["privacy_tier"] != "Confidential compute"


def test_provider_cards_compare_upstream_verification_not_gateway(client: TestClient) -> None:
    response = client.get("/providers")
    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    cards = soup.select("[data-provider-row]")
    assert {card["data-provider-id"] for card in cards} == set(PROVIDERS) - {"trustedrouter"}
    assert soup.select_one("[data-provider-result-count]").get_text(strip=True) == f"{len(cards)} entries"
    assert cards[0]["data-provider-id"] == "tinfoil"
    for card in cards:
        provider = PROVIDERS[str(card["data-provider-id"])]
        facts = {
            row.dt.get_text(strip=True): row.dd.get_text(" ", strip=True)
            for row in card.select(".provider-card-facts > div")
        }
        assert "Confidential compute" not in facts
        assert "Provider E2EE" not in facts
        assert "Zero data retention" in facts
        verified = provider.provider_confidential_compute is True and provider.provider_e2ee is True
        assert facts["Verified confidential inference"] == ("Verified" if verified else "Not verified")


@pytest.mark.parametrize("slug", ["phala", "confidential-ai", "zero-g", "openai", "tinfoil"])
def test_provider_details_use_the_same_verification_rule(client: TestClient, slug: str) -> None:
    response = client.get(f"/providers/{slug}")
    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    facts = {
        row.th.get_text(strip=True): row.td.get_text(" ", strip=True)
        for row in soup.select(".panel-body table")[0].select("tr")
    }
    assert "Confidential compute" not in facts
    assert "Provider E2EE" not in facts
    provider = PROVIDERS[slug]
    verified = provider.provider_confidential_compute is True and provider.provider_e2ee is True
    assert facts["Verified confidential inference"] == ("Verified" if verified else "Not verified")


def test_gateway_detail_does_not_claim_all_upstream_inference_is_verified(client: TestClient) -> None:
    response = client.get("/providers/trustedrouter")
    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    labels = {cell.get_text(strip=True) for cell in soup.select(".panel-body table th")}
    assert "Confidential compute" not in labels
    assert "Provider E2EE" not in labels
    assert "Verified confidential inference" not in labels
    assert "Gateway only" in response.text
