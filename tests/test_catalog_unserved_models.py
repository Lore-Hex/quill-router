"""GET /v1/models lists only models some route can serve.

A model row with no prepaid route and no BYOK route answers every chat
request with 400 "No route candidates match the requested provider filters".
The 2026-11-20 BYOK removal left rows like that behind (the dashed Claude ids
such as `anthropic/claude-haiku-4-5` whose only route was a DeepInfra BYOK
endpoint, `-aws`/`-coding`/`-ccmax` variants, dated ByteDance ids), and
clients that read the catalog (models.dev's sync, model pickers) offered them.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from trusted_router.catalog import MODELS, NAMED_DECISION_MODEL_PROVIDERS, model_to_openrouter_shape


def _servable(trustedrouter: dict[str, object]) -> bool:
    return bool(trustedrouter.get("prepaid_available") or trustedrouter.get("byok_available"))


def test_the_public_list_omits_models_no_route_can_serve(client: TestClient) -> None:
    listed = {row["id"]: row for row in client.get("/v1/models").json()["data"]}
    unserved = {
        model_id
        for model_id, model in MODELS.items()
        if not _servable(model_to_openrouter_shape(model)["trustedrouter"])
    }
    # Positive control: the shipped catalog has such rows, so the check below
    # is not vacuous.
    assert unserved
    assert not unserved & listed.keys()
    assert all(_servable(row["trustedrouter"]) for row in listed.values())
    # The full form and the count agree with the default list.
    full = client.get("/v1/models", params={"include": "endpoints"}).json()["data"]
    assert {row["id"] for row in full} == listed.keys()
    assert client.get("/v1/models/count").json()["data"]["count"] == len(listed)


def test_rows_served_without_endpoints_of_their_own_stay_listed(client: TestClient) -> None:
    listed = {row["id"] for row in client.get("/v1/models").json()["data"]}
    # Routing pools are served by their members.
    assert "trustedrouter/auto" in listed
    # A named decision model is served from its backing model's routes while
    # a chain host can serve it; it stays listed exactly then.
    for model_id in NAMED_DECISION_MODEL_PROVIDERS:
        if model_id not in MODELS:
            continue
        shape = model_to_openrouter_shape(MODELS[model_id])["trustedrouter"]
        assert (model_id in listed) == _servable(shape), model_id
