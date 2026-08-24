"""The auto ladder must have exactly one source of truth.

`Settings.auto_model_order` used to carry its own copy of the ladder. Because
`auto_candidate_models()` only falls back to `DEFAULT_AUTO_MODEL_ORDER` when the
setting is empty, the copy in config always won: the documented ladder in
`catalog_data.py` governed only the advertised catalog, never routing.

The two drifted, and the drift was invisible. `trustedrouter/auto` kept routing
to `anthropic/claude-opus-4.7` — the most expensive model in the catalog, and
one that was 502ing at settlement — while the documented default led with a
cheap one. Nothing failed; editing the documented ladder simply had no effect
on where requests went.

These tests fail if any second copy is reintroduced.
"""

from __future__ import annotations

from trusted_router.catalog_data import DEFAULT_AUTO_MODEL_ORDER
from trusted_router.config import Settings
from trusted_router.routing_candidates import auto_candidate_models


def test_deployed_default_routes_by_the_documented_ladder() -> None:
    """What a default-configured deployment actually routes on must be what
    `DEFAULT_AUTO_MODEL_ORDER` says, not a second list that quietly outranks it.
    """
    deployed = [model.id for model in auto_candidate_models(Settings().auto_model_order)]
    documented = [model.id for model in auto_candidate_models(None)]
    assert deployed == documented


def test_documented_ladder_survives_catalog_filtering() -> None:
    """Guard the comparison above from passing vacuously: if every ID were
    filtered out as unknown, both sides would be equal and empty."""
    assert len(auto_candidate_models(None)) >= 3


def test_auto_leads_with_the_current_deepseek_release() -> None:
    """The global ladder must start with the explicitly pinned 0813 release."""
    assert DEFAULT_AUTO_MODEL_ORDER[0] == "deepseek/deepseek-v4-pro-0813"
    assert auto_candidate_models(None)[0].id == "deepseek/deepseek-v4-pro-0813"


def test_explicit_override_still_wins() -> None:
    """Emptying the setting must not cost operators the runtime override."""
    override = "openai/gpt-4.1-mini,google/gemini-2.5-flash"
    resolved = [model.id for model in auto_candidate_models(override)]
    assert resolved == ["openai/gpt-4.1-mini", "google/gemini-2.5-flash"]


def test_config_holds_no_second_copy_of_the_ladder() -> None:
    """The failure mode was a hardcoded ladder in config outranking the
    documented one. Empty is the only value that keeps a single source."""
    assert Settings().auto_model_order == ""
