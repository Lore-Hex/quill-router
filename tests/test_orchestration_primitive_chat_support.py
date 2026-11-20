"""Catalog chat support must match what a bare chat request gets.

The gateway refuses a plain chat request (one user message, no tools, no
configuration) naming trustedrouter/synth, synth-code, selector, advisor or
subagent with 400, because these primitives have no built-in configuration.
They were published with `supports_chat: true`, so chat pickers offered them
and the first request failed. trustedrouter/mapreduce has defaults and serves
a bare request, so it stays chat-capable.
"""

from __future__ import annotations

from trusted_router.catalog import (
    ADVISOR_MODEL_ID,
    AUTO_MODEL_ID,
    IRIS_MODEL_ID,
    MAPREDUCE_MODEL_ID,
    MODELS,
    ORCHESTRATION_PRIMITIVE_MODEL_IDS,
    SELECTOR_MODEL_ID,
    SOCRATES_1_0_MODEL_ID,
    SUBAGENT_MODEL_ID,
    SYNTH_CODE_MODEL_ID,
    SYNTH_MODEL_ID,
    model_to_openrouter_shape,
    offers_chat,
    orchestration_role,
)
from trusted_router.catalog_data import (
    ORCHESTRATION_PRIMITIVE_REQUIRED_CONFIGURATION,
    serves_bare_chat,
)
from trusted_router.dashboard import _model_detail_view

REFUSES_BARE_CHAT = {
    SYNTH_MODEL_ID: ["preset or analysis_models"],
    SYNTH_CODE_MODEL_ID: ["preset or analysis_models"],
    SELECTOR_MODEL_ID: ["selector_models, preset or analysis_models"],
    ADVISOR_MODEL_ID: ["worker_models", "advisor_models"],
    SUBAGENT_MODEL_ID: ["controller_model"],
}


def test_every_primitive_declares_its_bare_request_behavior() -> None:
    # A new primitive must say whether it needs configuration; an omission
    # would silently advertise it as plain chat.
    assert set(ORCHESTRATION_PRIMITIVE_REQUIRED_CONFIGURATION) == set(
        ORCHESTRATION_PRIMITIVE_MODEL_IDS
    )
    for model_id in ORCHESTRATION_PRIMITIVE_MODEL_IDS:
        assert orchestration_role(model_id) == "primitive"


def test_primitives_without_defaults_do_not_advertise_plain_chat() -> None:
    for model_id, required in REFUSES_BARE_CHAT.items():
        model = MODELS[model_id]
        tr = model_to_openrouter_shape(model)["trustedrouter"]
        assert tr["orchestration_role"] == "primitive", model_id
        assert tr["supports_chat"] is False, model_id
        assert tr["supports_responses"] is False, model_id
        assert tr["required_configuration"] == required, model_id
        # Still usable on the chat route once configured.
        assert offers_chat(model)
        assert not serves_bare_chat(model)


def test_mapreduce_serves_a_bare_chat_request() -> None:
    tr = model_to_openrouter_shape(MODELS[MAPREDUCE_MODEL_ID])["trustedrouter"]
    assert tr["orchestration_role"] == "primitive"
    assert tr["supports_chat"] is True
    assert "required_configuration" not in tr


def test_named_presets_and_routers_stay_chat_capable() -> None:
    for model_id in (AUTO_MODEL_ID, IRIS_MODEL_ID, SOCRATES_1_0_MODEL_ID):
        tr = model_to_openrouter_shape(MODELS[model_id])["trustedrouter"]
        assert tr["supports_chat"] is True, model_id
        assert "required_configuration" not in tr


def test_public_catalog_and_model_page_agree(client) -> None:  # type: ignore[no-untyped-def]
    listed = {
        row["id"]: row for row in client.get("/v1/models").json()["data"]
    }
    for model_id in (*REFUSES_BARE_CHAT, MAPREDUCE_MODEL_ID):
        expected = model_id == MAPREDUCE_MODEL_ID
        if model_id in listed:
            assert listed[model_id]["trustedrouter"]["supports_chat"] is expected, model_id
        view = _model_detail_view(MODELS[model_id], test_mode=True)
        assert view["supports_chat"] is expected, model_id
