"""A model's publisher is its author, not whichever host serves it first.

Model.provider is the default route. For an author without its own route
mapping (catalog_ingest._AUTHOR_TO_PROVIDER_SLUG) it is the first host that
lists the model, so model pages named DeepInfra as the publisher of MiniMax M3,
and delisting MiniMax changed nothing else on the page.
"""

from __future__ import annotations

from dataclasses import replace

from trusted_router.catalog import MODELS
from trusted_router.dashboard import _model_publisher

_TEMPLATE = next(iter(MODELS.values()))


def _model(model_id: str, provider: str):
    return replace(_TEMPLATE, id=model_id, provider=provider)


def test_an_author_with_a_provider_entry_publishes_its_models_on_any_host() -> None:
    assert _model_publisher(_model("minimax/fixture-model", "deepinfra")).slug == "minimax"
    assert _model_publisher(_model("cohere/fixture-model", "azure")).slug == "cohere"


def test_an_author_without_one_falls_back_to_the_default_route() -> None:
    # Positive control: with no provider entry for the author, there is
    # nothing to name but the default route.
    assert _model_publisher(_model("fixture-author/fixture-model", "novita")).slug == "novita"
