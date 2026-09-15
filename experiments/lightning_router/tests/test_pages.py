from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from lightning_router.app import create_app
from lightning_router.catalog import limits, pricing, public_price


def test_release_includes_both_frontend_entrypoints_and_shared_dependencies():
    root = Path(__file__).resolve().parents[1]
    allowlist = (root / ".dockerignore").read_text().splitlines()
    for name in ("frontend.js", "pages.js", "setup.mjs", "session.mjs"):
        assert "!" + name in allowlist
        assert (root / name).is_file()
    dockerfile = (root / "Dockerfile").read_text()
    assert "/build/web/app.js ./web/app.js" in dockerfile
    assert "/build/web/pages.js ./web/pages.js" in dockerfile


@pytest.mark.parametrize("path,title", [("/usage", "Usage"), ("/pricing", "Pricing"), ("/docs", "Docs"), ("/terms", "Terms of Service"), ("/privacy", "Privacy Policy")])
def test_public_pages_have_navigation_and_need_no_payment(path, title, funding):
    with TestClient(create_app(funding, network="regtest", start_worker=False)) as client:
        response = client.get(path)
    assert response.status_code == 200
    assert f"<h1>{title}</h1>" in response.text
    for link in ("/", "/usage", "/pricing", "/docs", "/terms", "/privacy"):
        assert f'href="{link}"' in response.text
    assert "{{" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert funding.lnd.creates == 0
    assert funding.credits.balances == {}


@pytest.mark.parametrize("path", ["/", "/usage", "/pricing", "/docs", "/terms", "/privacy"])
def test_community_links_are_public_static_and_need_no_payment(path, funding):
    with TestClient(create_app(funding, network="regtest", start_worker=False)) as client:
        response = client.get(path)
    for link in (
        "https://x.com/lightningrouter", "https://x.com/trustedrouter",
        "https://discord.gg/FREVts9KAG", "https://github.com/Lore-Hex/lightning-router",
        "https://github.com/Lore-Hex/lightning-router/issues",
    ):
        assert f'href="{link}" rel="noopener noreferrer"' in response.text
    assert "<iframe" not in response.text
    assert funding.lnd.creates == 0
    assert funding.credits.balances == {}


def test_usage_authentication_cannot_create_or_fund_account(funding, raw_key):
    with TestClient(create_app(funding, network="regtest", start_worker=False)) as client:
        assert client.get("/api/usage").status_code == 401
        assert client.get("/api/usage", headers={"Authorization": "Bearer " + raw_key}).status_code == 401
        assert funding.credits.balances == {}
        funding.credits.resolve(raw_key, new=True)
        response = client.get("/api/usage", headers={"Authorization": "Bearer " + raw_key})
        assert response.json()["usage_usd"] == "1.234567"
        assert raw_key not in response.text
        assert funding.lnd.creates == 0
        account = funding.credits.resolve(raw_key, new=False)
        funding.credits.revoked.add(account)
        assert client.get("/api/usage", headers={"Authorization": "Bearer " + raw_key}).status_code == 401


def test_deepseek_documents_default_separately_from_maximum():
    result = limits({"id": "deepseek/deepseek-v4.1-flash", "context_length": 1048576,
                     "top_provider": {"max_completion_tokens": None}})
    assert result["output"] == 393216
    assert result["default_output"] == 65536
    assert result["limits_source"].startswith("https://api-docs.deepseek.com/")
    assert limits({"id": "unknown/model"})["output"] is None


def test_upstream_usage_corruption_is_a_service_error_not_invalid_key(funding, raw_key, monkeypatch):
    def invalid(_):
        raise ValueError("private response content " + raw_key)
    monkeypatch.setattr(funding.credits, "usage", invalid)
    with TestClient(create_app(funding, network="regtest", start_worker=False)) as client:
        response = client.get("/api/usage", headers={"Authorization": "Bearer " + raw_key})
    assert response.status_code == 503
    assert response.json() == {"error": "temporarily_unavailable"}
    assert raw_key not in response.text


def test_explicit_catalog_limits_take_precedence_and_fit_context():
    result = limits({"id": "deepseek/deepseek-v4.1-flash", "context_length": 32768,
                     "top_provider": {"max_completion_tokens": 8192}})
    assert result["output"] == result["default_output"] == 8192


def test_prices_are_decimal_and_include_non_token_charges():
    result = pricing({"pricing": {"prompt": "0.0000000422", "completion": "0.0000000844"},
                      "trustedrouter": {"request_price_microdollars": 25, "minimum_charge_microdollars": 1}})
    assert result["input_per_million"] == "0.0422000000"
    assert result["output_per_million"] == "0.0844000000"
    assert result["request_usd"] == "0.000025"
    assert result["minimum_charge_usd"] == "0.000001"
    assert result["cached_input_per_million"] is None


@pytest.mark.parametrize("value", [None, True, -1, "NaN", "Infinity", "1e-999999999", "oops", {}])
def test_unknown_or_invalid_price_is_not_zero(value):
    assert public_price(value) is None
