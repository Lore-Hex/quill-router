from trusted_router.catalog import MODEL_ENDPOINTS, PROVIDERS, endpoint_for_id
from trusted_router.catalog_registry import HELD_PREPAID_SETTLEMENT_ENDPOINTS
from trusted_router.provider_contracts import PREPAID_PROVIDER_HOLD_REASONS
from trusted_router.routes.internal.gateway import _endpoint_for_id_compat


def test_exhausted_operator_account_cannot_receive_new_prepaid_requests():
    assert PREPAID_PROVIDER_HOLD_REASONS.get("inceptron") == "operator-credit-exhausted"
    assert "inceptron" in PROVIDERS
    assert not any(
        endpoint.provider == "inceptron" and endpoint.usage_type == "Credits"
        for endpoint in MODEL_ENDPOINTS.values()
    )
    assert any(
        endpoint.model_id == "z-ai/glm-5.3-flash" and endpoint.provider != "inceptron"
        and endpoint.usage_type == "Credits"
        for endpoint in MODEL_ENDPOINTS.values()
    )


def test_existing_inceptron_authorizations_keep_their_settlement_tariffs():
    held = [endpoint for endpoint in HELD_PREPAID_SETTLEMENT_ENDPOINTS.values()
            if endpoint.provider == "inceptron"]
    assert held
    for endpoint in held:
        assert endpoint.usage_type == "Credits"
        assert endpoint_for_id(endpoint.id) is None
        assert _endpoint_for_id_compat(endpoint.id) == endpoint
