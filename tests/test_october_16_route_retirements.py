

from trusted_router import catalog


def test_lightning_security_incident_blocks_all_credentials_and_new_models():
    from trusted_router.provider_contracts import provider_model_operator_held

    for model in ("lightning-ai/glm-5.3", "lightning-ai/future-model"):
        assert provider_model_operator_held("lightning", model, model)
    assert not provider_model_operator_held("other", "lightning-ai/glm-5.3")
    assert not [e for e in catalog.MODEL_ENDPOINTS.values() if e.provider == "lightning"]
