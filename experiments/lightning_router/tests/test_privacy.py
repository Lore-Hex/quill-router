import httpx
from lightning_router.app import Catalog


def endpoint(provider, **overrides):
    return {"provider": provider, "usage_type": "Credits", "supported_parameters": ["tools"], **overrides}


def catalog_privacy(endpoints, **policy):
    row = {"id": "test/model", "supported_parameters": ["tools"], "trustedrouter": {
        "prepaid_available": True, "supports_chat": True, "endpoints": endpoints, **policy}}
    catalog = Catalog(httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"data": [row]}))))
    return catalog.current()[0]["privacy"]


def test_privacy_uses_prepaid_tool_endpoints_not_publisher_or_max_tier():
    privacy = catalog_privacy([
        endpoint("tee", provider_confidential_compute=True, provider_e2ee=True),
        endpoint("zdr", provider_zero_data_retention=True, stores_content=False),
        endpoint("no-store", stores_content=False),
        endpoint("unknown"),
        endpoint("byok", usage_type="BYOK", provider_confidential_compute=True, provider_e2ee=True),
        endpoint("no-tools", supported_parameters=[], provider_confidential_compute=True, provider_e2ee=True),
    ], privacy_tier=3, provider_e2ee=True, provider_zero_data_retention=True)
    assert privacy == {"any": ["no-store", "tee", "unknown", "zdr"],
                       "no_store": ["no-store", "zdr"], "zdr": ["zdr"], "confidential": ["tee"]}


def test_unknown_or_malformed_privacy_never_earns_a_guarantee():
    privacy = catalog_privacy([
        endpoint("partial", provider_e2ee=True),
        endpoint("strings", provider_e2ee="true", provider_confidential_compute="true",
                 provider_zero_data_retention="true", stores_content=0),
        endpoint("bad/provider", provider_e2ee=True, provider_confidential_compute=True),
        None, "invalid",
    ])
    assert privacy == {"any": ["partial", "strings"], "no_store": [], "zdr": [], "confidential": []}
    for invalid in (None, {}, "invalid", []):
        assert catalog_privacy(invalid)["confidential"] == []


def test_endpoints_are_deduplicated_and_explicit_flags_can_overlap():
    row = endpoint("private", provider_e2ee=True, provider_confidential_compute=True,
                   provider_zero_data_retention=True, stores_content=False)
    assert catalog_privacy([row, row]) == {key: ["private"] for key in ("any", "no_store", "zdr", "confidential")}
