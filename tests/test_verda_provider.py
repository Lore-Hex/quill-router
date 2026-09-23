from trusted_router.catalog import (
    GATEWAY_PREPAID_PROVIDER_SLUGS,
    MODEL_ENDPOINTS,
    PROVIDERS,
    provider_to_openrouter_shape,
)
from trusted_router.dashboard import public_provider_detail_html


def test_verda_is_visible_but_cannot_route_unverified_models() -> None:
    provider = PROVIDERS["verda"]
    public = provider_to_openrouter_shape(provider)

    assert public["name"] == "Verda"
    assert public["routing_status"] == "blocked"
    assert "no-default-models" in public["routing_status_reason"]
    assert provider.supports_prepaid is False
    assert provider.supports_byok is False
    assert "verda" not in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert all(endpoint.provider != "verda" for endpoint in MODEL_ENDPOINTS.values())


def test_verda_does_not_inherit_confidentiality_from_cloud_marketing() -> None:
    provider = PROVIDERS["verda"]

    assert provider.stores_content is True
    assert provider.provider_zero_data_retention is not True
    assert provider.prepaid_zero_data_retention is False
    assert provider.provider_confidential_compute is not True
    assert provider.provider_e2ee is not True
    assert provider.provider_headquarters_country == "FI"


def test_verda_provider_page_explains_activation_requirements(test_settings) -> None:  # noqa: ANN001
    html = public_provider_detail_html(test_settings, "verda")

    assert html is not None
    assert "no-default-models" in html
    assert "billable token prices" in html
    assert "https://verda.com/" in html
    assert "https://api.verda.com/v1/docs" in html
