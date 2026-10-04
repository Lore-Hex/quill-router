from __future__ import annotations

from decimal import Decimal

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from trusted_router.catalog import ModelEndpoint
from trusted_router.money import (
    dollars_to_microdollars,
    microdollars_to_decimal,
    token_cost_microdollars,
)
from trusted_router.stage_d import (
    endpoint_cost_microdollars_from_candidate,
    endpoint_pricing_candidate,
)


@pytest.mark.parametrize("path", ["/docs", "/docs/spend-controls"])
def test_docs_render_two_hosts_and_management_example(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    sections = page.select("#two-hosts")
    assert len(sections) == 1
    section = sections[0]
    rows = section.select("tbody tr")
    assert len(rows) == 2
    assert [row.select_one("td").get_text(strip=True) for row in rows] == [
        "https://api.trustedrouter.com/v1",
        "https://trustedrouter.com/v1",
    ]
    inference = rows[0].get_text(" ", strip=True)
    for endpoint in ("chat/completions", "responses", "messages", "embeddings", "models", "key"):
        assert f"/v1/{endpoint}" in inference
    management = rows[1].get_text(" ", strip=True)
    for text in ("POST /v1/keys", "GET /v1/keys", "PATCH", "DELETE /v1/keys/{hash}", "credits", "activity", "workspaces"):
        assert text in management
    assert "GET /v1/key also works here" in management
    assert section.select_one("pre").get_text() == (
        'curl https://trustedrouter.com/v1/keys \\\n'
        '  -H "Authorization: Bearer $TR_MANAGEMENT_KEY"'
    )
    assert "Management calls to the inference host return 404" in section.get_text(" ", strip=True)


@pytest.mark.parametrize("path", ["/docs", "/pricing"])
def test_docs_render_minimum_charge_and_rounding_rules(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    sections = page.select("#minimum-charges")
    assert len(sections) == 1
    text = sections[0].get_text(" ", strip=True)
    for rule in (
        "1 microdollar = $0.000001",
        "5.5%",
        "rounded up to a whole microdollar per million tokens",
        "$0.01 per million token price floor",
        "do not add 5.5% again",
        "input, output, cache read, and cache write separately",
        "half up",
        "1 microdollar minimum per billed request",
        "With no chargeable usage and no fee, the charge is zero",
        "Perplexity routes include a per-request fee",
        "Parasail Liberty 2.0",
        "$0.001 minimum",
    ):
        assert rule in text
    assert len(sections[0].select("#billing-examples tbody tr")) == 2


@pytest.mark.parametrize("path", ["/docs", "/pricing"])
@pytest.mark.parametrize(
    ("row_index", "input_tokens", "output_tokens", "input_price", "output_price", "expected_raw", "expected_components", "expected_bill", "expected_multiple", "rounding_note"),
    [
        (0, 10, 5, "0.01", "0.02", "0.2", (0, 0), 1, "5", "; request minimum applies"),
        (1, 30, 10, "0.05", "0.05", "2", (2, 1), 3, "1.5", " (1.5 and 0.5 each round up)"),
    ],
)
def test_documented_examples_match_real_billing(
    client: TestClient,
    path: str,
    row_index: int,
    input_tokens: int,
    output_tokens: int,
    input_price: str,
    output_price: str,
    expected_raw: str,
    expected_components: tuple[int, int],
    expected_bill: int,
    expected_multiple: str,
    rounding_note: str,
) -> None:
    endpoint = ModelEndpoint(
        id="documented-example",
        model_id="documented-example",
        provider="openai",
        usage_type="prepaid",
        prompt_price_microdollars_per_million_tokens=dollars_to_microdollars(input_price),
        completion_price_microdollars_per_million_tokens=dollars_to_microdollars(output_price),
    )
    candidate = endpoint_pricing_candidate(endpoint)
    bill = endpoint_cost_microdollars_from_candidate(candidate, input_tokens, output_tokens)
    components = (
        token_cost_microdollars(input_tokens, endpoint.prompt_price_microdollars_per_million_tokens),
        token_cost_microdollars(output_tokens, endpoint.completion_price_microdollars_per_million_tokens),
    )
    raw = input_tokens * Decimal(input_price) + output_tokens * Decimal(output_price)
    assert raw == Decimal(expected_raw)
    assert components == expected_components
    assert bill == expected_bill
    assert Decimal(bill) / raw == Decimal(expected_multiple)

    response = client.get(path)
    assert response.status_code == 200
    rows = BeautifulSoup(response.text, "html.parser").select("#billing-examples tbody tr")
    assert len(rows) == 2
    unit = "microdollar" if bill == 1 else "microdollars"
    assert [cell.get_text(" ", strip=True) for cell in rows[row_index].select("td")] == [
        f"{input_tokens} input + {output_tokens} output tokens",
        f"${input_price} input / ${output_price} output",
        f"{raw.normalize():f} microdollars",
        f"{components[0]} input + {components[1]} output{rounding_note}",
        f"{bill} {unit} (${microdollars_to_decimal(bill)})",
        f"{Decimal(bill) / raw:g}×",
    ]
