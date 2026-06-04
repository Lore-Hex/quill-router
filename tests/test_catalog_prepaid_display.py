from __future__ import annotations

from trusted_router.catalog import MODELS, endpoints_for_model
from trusted_router.dashboard import _model_detail_view


def _a_supplemental_priced_model() -> str:
    """A model with raw prepaid_available=False but a real Credits endpoint —
    i.e. a supplemental provider-native model that IS prepaid-routable."""
    for model in MODELS.values():
        if model.prepaid_available:
            continue
        if any(e.usage_type == "Credits" for e in endpoints_for_model(model.id)):
            return model.id
    raise AssertionError("expected at least one supplemental priced model")


def test_supplemental_model_surfaces_as_prepaid_on_detail() -> None:
    model_id = _a_supplemental_priced_model()
    model = MODELS[model_id]
    # Premise: the raw catalog flag is a dedup marker (False)...
    assert model.prepaid_available is False
    # ...but the rendered detail view derives prepaid from endpoints → True.
    view = _model_detail_view(model)
    assert view["prepaid"] is True


def test_byok_only_model_stays_not_prepaid() -> None:
    # A model with no Credits endpoint and raw flag False must NOT flip to
    # prepaid (the `or model.prepaid_available` fallback is still conservative).
    for model in MODELS.values():
        if model.prepaid_available:
            continue
        if not any(e.usage_type == "Credits" for e in endpoints_for_model(model.id)):
            assert _model_detail_view(model)["prepaid"] is False
            return
    # If every non-prepaid model has a Credits endpoint, there's nothing to
    # assert — not a failure.
