from trusted_router.catalog import MODELS
from trusted_router.routes.catalog import _public_catalog_payload


def test_a_window_stated_on_any_route_reaches_the_model() -> None:
    # Created from a provider row with no window while another provider's
    # row states one; the model used to publish 0.
    assert MODELS["google/gemma-4-12b-it"].context_length == 32768


def test_published_catalog_never_states_a_zero_window() -> None:
    rows = _public_catalog_payload((0, "context-window-regression")).shapes
    unknown = []
    for row in rows:
        assert row["context_length"] != 0, row["id"]
        assert row["top_provider"]["context_length"] != 0, row["id"]
        if row["context_length"] is None:
            unknown.append(row["id"])
    # An unknown window is published as null rather than dropped.
    assert "cohere/command-a" in unknown
