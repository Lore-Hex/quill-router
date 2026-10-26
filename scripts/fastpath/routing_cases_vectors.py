"""Golden routing cases for the Go authorize evaluator (E2, docs/design/routing-snapshot-v1.md).

Each case is a request evaluated by ``trusted_router.authorize_candidates`` (the
candidate pipeline authorize runs) over the routing-snapshot fixture catalog:
the synthetic models, endpoints, rank evidence and rank preferences that
scripts/fastpath/routing_snapshot_vectors.py compiles into
fastpath/testdata/routing_snapshot_v3.body.json. It maps to the candidate
endpoint ids in order, the model estimate in microdollars, the normalized
service tier and the Stage D reason, or to the refusal's status, type and
message. Nothing here reads the live catalog or the wall clock, so the hourly
price refresh cannot make the file stale; a change to the pipeline or to a
catalog rule it applies does, and tests/test_fastpath_routing_cases_vectors.py
says so.

The process-local health re-rank is absent, as for a freshly started
instance. The script installs the fixture catalog and pins the lifecycle,
catalog and rank clocks to each case's instant in its own process; run it as a
script, never import it into a process that serves or tests other code.

    uv run python scripts/fastpath/routing_cases_vectors.py
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
# Settings must not take routing inputs from the shell that runs this.
for _name in [name for name in os.environ if name.startswith("TR_")]:
    del os.environ[_name]
os.environ["TR_ENVIRONMENT"] = "test"
os.environ["TR_STORAGE_BACKEND"] = "memory"

from fastapi import HTTPException  # noqa: E402

from trusted_router import catalog_data, provider_lifecycle, provider_ranking, routing  # noqa: E402
from trusted_router.authorize_candidates import (  # noqa: E402
    CandidateRequest,
    NamedChainRefusal,
    finish_candidates,
    plan_route,
    resolve_candidates,
)
from trusted_router.catalog_registry import MODEL_ENDPOINTS, MODELS  # noqa: E402
from trusted_router.config import Settings  # noqa: E402
from trusted_router.regions import choose_region  # noqa: E402
from trusted_router.schemas import GatewayAuthorizeRequest  # noqa: E402

OUTPUT = ROOT / "fastpath" / "testdata" / "routing_cases_v1.json"
GENERATOR = "scripts/fastpath/routing_cases_vectors.py"
SNAPSHOT = "fastpath/testdata/routing_snapshot_v3.body.json"
REGIONS = ("us-central1", "us-east4", "europe-west4", "us-west1")
PRIMARY_REGION = "us-central1"
# What a request in the E2 cohort brings to the Stage D decision beyond the
# candidates: a pilot workspace, an accepted boot that declares heartbeats,
# standard endpoint pricing, the typed settlement backend.
STAGE_D_INPUTS = {
    "eligibility_enabled": True, "pilot": True, "heartbeat_enabled": True,
    "boot_accepted": True, "heartbeat_declared": True,
    "require_heartbeat_declaration": False, "standard_endpoint_pricing": True,
    "settlement_backend": True,
}
# Inside the snapshot's window, [00:00, 05:30) on 2026-10-19 (a Monday):
# DeepSeek and Tencent are off peak, at peak, and off peak again.
INSTANTS = {
    "off_peak": "2026-10-19T00:30:00Z",
    "peak": "2026-10-19T02:00:00Z",
    "late": "2026-10-19T04:30:00Z",
}


def _snapshot_vectors() -> Any:
    path = ROOT / "scripts" / "fastpath" / "routing_snapshot_vectors.py"
    spec = importlib.util.spec_from_file_location("routing_snapshot_vectors", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _instant(stamp: str) -> dt.datetime:
    return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))


@contextmanager
def fixture_catalog(at: dt.datetime) -> Iterator[None]:
    """Install the snapshot fixture's catalog and policy; pin every clock to ``at``."""
    inputs = _snapshot_vectors().fixture_inputs()
    aliases = inputs.aliases
    saved_models, saved_endpoints = dict(MODELS), dict(MODEL_ENDPOINTS)

    class _Clock(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:  # type: ignore[override]
            return at if tz is None else at.astimezone(tz)

    patches: list[tuple[Any, str, Any]] = [
        (provider_lifecycle, "_utc_now", lambda: at),
        (catalog_data, "_utc_now", lambda: at),
        (provider_ranking, "datetime", _Clock),
        (provider_ranking, "_RANKS", provider_ranking.validate_snapshot(inputs.ranking_evidence)[1]),
        (provider_ranking, "_GENERATED_AT", provider_ranking.validate_snapshot(inputs.ranking_evidence)[0]),
        (routing, "_MODEL_PROVIDER_PREFERENCE", {m: dict(p) for m, p in inputs.model_provider_preference.items()}),
        (routing, "_PREPAID_PROVIDER_MIN_RANK", dict(inputs.prepaid_min_rank)),
        (routing, "_PROVIDER_ALIASES", dict(aliases.provider_aliases)),
        (routing, "_PROVIDER_GROUP_ALIASES", {k: tuple(v) for k, v in aliases.provider_group_aliases.items()}),
        (routing, "_ROUTER_PROVIDER_SLUGS", frozenset(aliases.router_provider_slugs)),
    ]
    originals = [(module, name, getattr(module, name)) for module, name, _value in patches]
    try:
        for module, name, value in patches:
            setattr(module, name, value)
        MODELS.clear()
        MODELS.update({model.id: model for model in inputs.models})
        MODEL_ENDPOINTS.clear()
        MODEL_ENDPOINTS.update({endpoint.id: endpoint for endpoint in inputs.endpoints})
        yield
    finally:
        for module, name, value in originals:
            setattr(module, name, value)
        MODELS.clear()
        MODELS.update(saved_models)
        MODEL_ENDPOINTS.clear()
        MODEL_ENDPOINTS.update(saved_endpoints)


# The routing fields of authorize's body_dict: the schema's fields routing
# reads, and the top-level allow_fallbacks, an extra field the schema keeps.
ROUTING_FIELDS = ("model", "models", "provider", "allow_fallbacks", "requested_parameters",
                  "input_modalities", "route_type", "region", "service_tier", "stream")


def _authorize_body(request: dict[str, Any]) -> tuple[GatewayAuthorizeRequest, dict[str, Any]]:
    """The request as the authorize schema admits it, and its routing body.

    GatewayAuthorizeRequest strips every string (str_strip_whitespace) and
    canonicalizes the model ids; extra fields such as allow_fallbacks stay as
    sent; authorize's body_dict is its dump without None. A case's
    output_tokens is what the schema derives from its max_* fields
    (output_estimate), written out so the file shows the number priced.
    """
    fields = {k: v for k, v in request.items() if k != "output_tokens"}
    body = GatewayAuthorizeRequest.model_validate({**fields, "api_key_lookup_hash": "routing-cases"})
    if request["output_tokens"] != body.output_estimate:
        raise ValueError(f"output_tokens must be the schema's output_estimate: {request}")
    dumped = body.model_dump(exclude_none=True)
    return body, {name: dumped[name] for name in ROUTING_FIELDS if name in dumped}


def evaluate(case: dict[str, Any]) -> dict[str, Any]:
    from trusted_router.routes.internal.gateway import _stage_d_eligibility_reason

    request = case["request"]
    at = _instant(case["at"])
    with fixture_catalog(at):
        settings = Settings(
            environment="test", regions=",".join(REGIONS), primary_region=PRIMARY_REGION,
            auto_model_order="",
        )
        body, routing_body = _authorize_body(request)
        region = choose_region(settings, body.region or None)
        candidate_request = CandidateRequest(
            route_type=body.route_type, route_model_id=body.model,
            routing_body=routing_body, body_region=body.region,
        )
        try:
            plan = plan_route(candidate_request, settings, region=region)
            # As the gateway calls it: the prompt estimate, and max_tokens (or
            # max_completion_tokens) as the completion the window must hold.
            resolved = resolve_candidates(
                plan, candidate_request, region=region, at=at,
                prompt_tokens=body.estimated_input_tokens,
                completion_tokens=body.max_tokens or body.max_completion_tokens or 0,
            )
            outcome = finish_candidates(
                plan, resolved,
                route_type=body.route_type,
                byok_providers=frozenset(case["byok_providers"]),
                estimated_input_tokens=body.estimated_input_tokens,
                output_tokens=body.output_estimate,
                service_tier=body.service_tier,
                additional_cost_reservation=0,
                video_resolution=None,
                at=at,
                rerank=None,
            )
        except NamedChainRefusal as refusal:
            return {"error": _error(refusal.error)}
        except HTTPException as exc:
            return {"error": _error(exc)}
        stage_d_reason = _stage_d_eligibility_reason(
            eligibility_enabled=STAGE_D_INPUTS["eligibility_enabled"],
            workspace_id="ws_test_pilot_a",
            pilot_workspace_ids=frozenset({"ws_test_pilot_a"}),
            heartbeat_enabled=STAGE_D_INPUTS["heartbeat_enabled"],
            boot_accepted=STAGE_D_INPUTS["boot_accepted"],
            heartbeat_declared=STAGE_D_INPUTS["heartbeat_declared"],
            require_heartbeat_declaration=STAGE_D_INPUTS["require_heartbeat_declaration"],
            stream=body.stream,
            route_type=body.route_type,
            endpoint_candidates=outcome.candidates,
            standard_endpoint_pricing=STAGE_D_INPUTS["standard_endpoint_pricing"],
            service_tier=outcome.service_tier,
            settlement_backend=STAGE_D_INPUTS["settlement_backend"],
        )
        return {
            "candidates": [endpoint.id for _model, endpoint in outcome.candidates],
            "model_estimate": outcome.model_estimate,
            "service_tier": outcome.service_tier,
            "stage_d_reason": stage_d_reason,
        }


def _error(exc: HTTPException) -> dict[str, Any]:
    error: dict[str, Any] = exc.detail["error"] if isinstance(exc.detail, dict) else {}
    out = {"status": exc.status_code, "type": error.get("type"), "message": error.get("message")}
    if "param" in error:
        out["param"] = error["param"]
    return out


FLASH, TENCENT_FLASH = "deepseek/deepseek-v4-flash", "deepseek/deepseek-v4.1-flash"
GLM, GEMINI = "z-ai/glm-5.3", "google/gemini-3.1-pro-preview"
FUGU, BEDROCK, INKLING = "sakana-ai/fugu-ultra-v1.1", "minimax/minimax-m2", "thinkingmachines/inkling"
KIMI = "moonshotai/kimi-k2.7"
MODEL_SPELLINGS = (
    FLASH, TENCENT_FLASH, GLM, GEMINI, FUGU, BEDROCK, INKLING,
    f"{FLASH}:nitro", f"{GEMINI}:floor", f"{GLM}-2025-04-14", "nope/unknown-model", "gpt-4.1",
)
PROVIDER_OPTIONS: tuple[tuple[str, dict[str, Any] | None], ...] = (
    ("none", None),
    ("order", {"order": ["deepinfra", "wafer"]}),
    ("only", {"only": ["wafer", "google-ai-studio"]}),
    ("ignore", {"ignore": ["deepseek", "tencent"]}),
    ("sort_price", {"sort": "price"}),
    ("sort_latency", {"sort": "latency"}),
    ("sort_throughput", {"sort": "throughput"}),
    ("no_fallbacks", {"allow_fallbacks": False}),
    ("deny_collection", {"data_collection": "deny"}),
    ("zdr", {"zdr": True}),
    ("confidential", {"min_privacy": "confidential"}),
    ("byok_only", {"usage": "byok"}),
    ("credits_only", {"usage": "credits"}),
    ("us_jurisdiction", {"jurisdiction": "us"}),
    ("max_price", {"max_price": {"prompt": 0.5, "completion": 2}}),
    ("alias", {"only": ["ai-studio"]}),
    ("group_alias", {"only": "google"}),
    ("router_name", {"only": ["openrouter"]}),
    ("unknown_provider", {"only": ["nope-provider"]}),
    ("perf", {"preferred_max_latency": 2}),
)


def cases() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def add(name: str, request: dict[str, Any], *, at: str = "peak", byok: tuple[str, ...] = ()) -> None:
        request = {"route_type": "chat.completions", "estimated_input_tokens": 1_000,
                   "output_tokens": 512, **request}
        out.append({"name": name, "at": INSTANTS[at], "byok_providers": sorted(byok),
                    "request": {k: v for k, v in request.items() if v is not None}})

    for model in MODEL_SPELLINGS:
        for at in INSTANTS:
            add(f"{model}@{at}", {"model": model, "stream": True}, at=at)
    for model in (FLASH, GEMINI, GLM, TENCENT_FLASH):
        for option, provider in PROVIDER_OPTIONS:
            add(f"{model}+{option}", {"model": model, "provider": provider})
    for region in ("us-central1", "europe-west4", "us-west1", "unknown-region", ""):
        for model in (FUGU, BEDROCK, FLASH):
            add(f"{model}@region:{region or 'empty'}", {"model": model, "region": region})
    for byok in ((), ("google-ai-studio",), ("deepseek", "wafer")):
        options: tuple[dict[str, Any] | None, ...] = (
            None, {"usage": "byok"}, {"order": ["google-ai-studio"], "allow_fallbacks": False},
        )
        for byok_option in options:
            add(f"{GEMINI}+byok:{','.join(byok) or 'none'}+{json.dumps(byok_option, sort_keys=True)}",
                {"model": GEMINI, "provider": byok_option}, byok=byok)
    for tier in ("default", "auto", "priority", " Priority ", "flex"):
        add(f"service_tier:{tier}", {"model": GEMINI, "service_tier": tier})
    for tokens in (0, 1, 200_000, 200_001, 2_000_000_000):
        add(f"{GEMINI}+tokens:{tokens}", {"model": GEMINI, "estimated_input_tokens": tokens})
    for route_type in (None, "responses", "messages", "embeddings", "decide", "images"):
        add(f"route:{route_type}", {"model": FLASH, "route_type": route_type})
    add("fallback_list", {"model": FLASH, "models": [GLM, GEMINI]})
    add("fallback_list_no_fallbacks", {"model": FLASH, "models": [GLM], "provider": {"allow_fallbacks": False}})
    add("fallback_list_sort_none", {"model": GLM, "models": [FLASH], "provider": {"sort": {"by": "price", "partition": "none"}}})
    add("required_parameters", {"model": GEMINI, "requested_parameters": ["tools"], "provider": {"require_parameters": True}})
    add("missing_parameters", {"model": GEMINI, "requested_parameters": ["logprobs"], "provider": {"require_parameters": True}})
    add("image_input", {"model": TENCENT_FLASH, "input_modalities": ["text", "image"]})
    add("image_input_unsupported", {"model": FLASH, "input_modalities": ["image"]})
    add("inkling_before_its_deadline", {"model": INKLING}, at="late")
    add("not_streaming", {"model": GEMINI, "stream": None})
    # Rules the cases above leave tied in the fixture, so that the Go
    # evaluator's order is held to Python's for each: provider.order as a
    # rank and, without fallbacks, as a ceiling; the prepaid floor where no
    # single-model preference replaces it; a variant suffix over the body's
    # sort and over an earlier suffix; the latency table where it differs
    # from the default one.
    add("order_reorders", {"model": GLM, "provider": {"order": ["fireworks"]}})
    add("order_ceiling_without_fallbacks", {"model": GLM, "provider": {"order": ["fireworks", "together"], "allow_fallbacks": False}})
    add("order_and_only_disjoint", {"model": GLM, "provider": {"order": ["fireworks"], "only": ["wafer"], "allow_fallbacks": False}})
    add("prepaid_floor_across_models", {"model": FLASH, "models": [TENCENT_FLASH]})
    add("variant_suffix_over_body_sort", {"model": f"{GLM}:floor", "provider": {"sort": "latency"}})
    add("variant_suffix_later_wins", {"model": f"{GLM}:floor", "models": [f"{FLASH}:nitro"]})
    add("latency_table_across_models", {"model": INKLING, "models": [GLM], "provider": {"sort": {"by": "latency", "partition": "none"}}})
    # The top-level allow_fallbacks, an extra field of the authorize body that
    # routing reads beside provider.allow_fallbacks.
    add("top_level_no_fallbacks", {"model": GLM, "allow_fallbacks": False})
    add("top_level_fallbacks", {"model": GLM, "allow_fallbacks": True})
    add("top_level_fallbacks_conflict", {"model": GLM, "allow_fallbacks": True, "provider": {"allow_fallbacks": False}})
    add("top_level_fallbacks_agree", {"model": GLM, "allow_fallbacks": False, "provider": {"allow_fallbacks": False}})
    add("top_level_fallbacks_not_bool", {"model": GLM, "allow_fallbacks": "false"})
    add("provider_fallbacks_not_bool", {"model": GLM, "provider": {"allow_fallbacks": 0}})
    add("router_name_then_fallbacks_not_bool", {"model": GLM, "allow_fallbacks": 1, "provider": {"only": ["openrouter"]}})
    # A provider list is validated whole before any item is looked up.
    add("router_name_then_blank_item", {"model": GLM, "provider": {"only": ["openrouter", ""]}})
    add("router_name_then_null_item", {"model": GLM, "provider": {"only": ["openrouter", None]}})
    # The authorize schema strips every string field (str_strip_whitespace).
    add("spaced_region", {"model": BEDROCK, "region": " us-east4 "})
    add("spaced_region_denied", {"model": FUGU, "region": " europe-west4"})
    # The context window (candidates_fitting_context): DeepSeek V4 Flash
    # holds 65,536 tokens, V4.1 Flash 32,768, GLM 5.3 131,072; Gemini's is
    # unknown (0) and always kept. The window holds the prompt estimate plus
    # max_tokens (else max_completion_tokens); max_output_tokens only prices.
    add("context_fits", {"model": FLASH, "estimated_input_tokens": 60_000, "max_tokens": 4_000, "output_tokens": 4_000})
    add("context_at_the_limit", {"model": FLASH, "estimated_input_tokens": 61_536, "max_tokens": 4_000, "output_tokens": 4_000})
    add("context_one_over", {"model": FLASH, "estimated_input_tokens": 61_537, "max_tokens": 4_000, "output_tokens": 4_000})
    add("context_prompt_at_the_limit", {"model": FLASH, "estimated_input_tokens": 65_536})
    add("context_prompt_one_over", {"model": FLASH, "estimated_input_tokens": 65_537})
    add("context_max_completion_tokens", {"model": FLASH, "estimated_input_tokens": 65_000, "max_completion_tokens": 537, "output_tokens": 537})
    add("context_max_tokens_wins", {"model": FLASH, "estimated_input_tokens": 65_000, "max_tokens": 536, "max_completion_tokens": 9_999, "output_tokens": 536})
    add("context_max_output_tokens_only_prices", {"model": FLASH, "estimated_input_tokens": 65_536, "max_output_tokens": 9_999, "output_tokens": 9_999})
    add("context_excludes_some_models", {"model": FLASH, "models": [GLM, TENCENT_FLASH], "estimated_input_tokens": 50_000})
    add("context_excludes_the_first_model", {"model": TENCENT_FLASH, "models": [FLASH], "estimated_input_tokens": 40_000})
    add("context_excludes_all", {"model": FLASH, "models": [TENCENT_FLASH], "estimated_input_tokens": 70_000, "max_tokens": 1_000, "output_tokens": 1_000})
    add("context_window_of_the_filtered", {"model": GLM, "models": [FLASH], "provider": {"only": ["deepseek"]}, "estimated_input_tokens": 70_000})
    add("context_unknown_window_kept", {"model": GEMINI, "models": [FLASH], "estimated_input_tokens": 100_000})
    add("context_before_region", {"model": BEDROCK, "estimated_input_tokens": 200_000})
    add("host_context_at_limit", {"model": GLM, "estimated_input_tokens": 99_999, "max_tokens": 1, "output_tokens": 1})
    add("host_context_one_over", {"model": GLM, "estimated_input_tokens": 100_000, "max_tokens": 1, "output_tokens": 1})
    add("host_context_pinned", {"model": GLM, "estimated_input_tokens": 100_001, "provider": {"only": ["wafer"]}})
    add("host_context_no_fallback", {"model": GLM, "estimated_input_tokens": 100_001, "provider": {"order": ["wafer", "together"], "allow_fallbacks": False}})
    add("spaced_model_and_route", {"model": f"  {GLM} ", "models": [f" {FLASH}"], "route_type": " chat.completions ",
                                   "stream": True})
    # provider.min_privacy: every accepted spelling (catalog_data.PRIVACY_TIER_ALIASES),
    # one spaced and cased, an unknown one, a blank, a null and two
    # non-strings, on GLM 5.3, whose hosts span the tiers: Fireworks stores
    # content, Wafer and Together are ZDR, Chutes Confidential, Tinfoil
    # Trusted. Confidential drops Wafer, the leader by rank and preference;
    # Trusted keeps Tinfoil alone.
    for spelling in ("standard", "any", "no_store", "no-store", "nostore", "zdr", "zero_retention",
                     "zero-retention", "confidential", "trusted", "e2e", "e2ee", "max", "maximum",
                     " Confidential ", "secret", ""):
        add(f"min_privacy:{spelling or 'blank'}", {"model": GLM, "provider": {"min_privacy": spelling}})
    add("min_privacy:null", {"model": GLM, "provider": {"min_privacy": None}})
    add("min_privacy:number", {"model": GLM, "provider": {"min_privacy": 2}})
    add("min_privacy:boolean", {"model": GLM, "provider": {"min_privacy": True}})
    # Privacy over a model list drops the first model's whole run (DeepSeek
    # V4 Flash has no ZDR host, so GLM 5.3's hosts lead), empties a model
    # with no host of the tier, composes with provider.zdr and with
    # provider.order (the survivors in the order's rank, not the catalog's),
    # keeps one candidate without fallbacks, and makes the soft
    # data_collection=deny hard, except at "any", which asks for nothing.
    # Kimi K2.7's preference for Chutes applies only when Kimi is the one
    # model left, so dropping V4 Flash reorders Kimi's hosts: Tinfoil leads
    # behind V4 Flash, Chutes once privacy removes it.
    add("min_privacy_leaves_the_second_model", {"model": FLASH, "models": [GLM], "provider": {"min_privacy": "zdr"}})
    add("min_privacy_empties", {"model": FLASH, "provider": {"min_privacy": "zdr"}})
    add("kimi_alone", {"model": KIMI})
    add("kimi_behind_flash", {"model": FLASH, "models": [KIMI]})
    add("min_privacy_reorders", {"model": FLASH, "models": [KIMI], "provider": {"min_privacy": "confidential"}})
    add("min_privacy_leaves_one_model", {"model": GLM, "models": [FLASH], "provider": {"min_privacy": "no_store"}})
    add("min_privacy_with_zdr_flag", {"model": GLM, "provider": {"min_privacy": "confidential", "zdr": True}})
    add("min_privacy_then_order", {"model": GLM, "provider": {"min_privacy": "zdr", "order": ["chutes", "together"]}})
    add("min_privacy_no_fallbacks", {"model": GLM, "provider": {"min_privacy": "confidential", "allow_fallbacks": False}})
    add("min_privacy_any_keeps_deny_soft", {"model": FLASH, "provider": {"data_collection": "deny", "min_privacy": "any"}})
    add("min_privacy_makes_deny_hard", {"model": FLASH, "provider": {"data_collection": "deny", "min_privacy": "no_store"}})
    # The refusals' order: the router name, then allow_fallbacks, then
    # min_privacy, then require_parameters.
    add("router_name_then_bad_min_privacy", {"model": GLM, "provider": {"only": ["openrouter"], "min_privacy": "secret"}})
    add("fallbacks_conflict_then_bad_min_privacy", {"model": GLM, "allow_fallbacks": True,
                                                    "provider": {"allow_fallbacks": False, "min_privacy": "secret"}})
    add("bad_min_privacy_then_bad_require_parameters", {"model": GLM, "provider": {"min_privacy": "secret", "require_parameters": "yes"}})
    # requested_parameters on a route type the list describes
    # (chat.completions, responses, none): a capability-required parameter
    # drops a route that does not declare it while another route of the
    # model does (tools: Together's GLM 5.3 and V4.1 Flash routes, the
    # latter the leader); structured output is held to the model only, so
    # Fireworks' route, declaring none, stays; a model none of whose routes
    # declare tools (Fugu) keeps every route; other names filter nothing;
    # names are stripped and blanks dropped. On messages the list describes
    # nothing. Pinned to a route without the capability, the refusal names
    # the parameter (and its param), or the parameters when more than one;
    # when the provider filters alone empty the list it is the plain refusal.
    add("parameters:tools", {"model": GLM, "requested_parameters": ["tools"]})
    add("parameters:tool_choice", {"model": GLM, "requested_parameters": ["tool_choice"]})
    add("parameters:response_format", {"model": GLM, "requested_parameters": ["response_format"]})
    add("parameters:structured_outputs", {"model": GLM, "requested_parameters": ["structured_outputs"]})
    add("parameters:tools_and_response_format", {"model": GLM, "requested_parameters": ["tools", "response_format"]})
    add("parameters:undeclared_model", {"model": FUGU, "requested_parameters": ["tools"]})
    add("parameters:drops_the_leader", {"model": TENCENT_FLASH, "requested_parameters": ["tools"]})
    add("parameters:across_models", {"model": TENCENT_FLASH, "models": [GLM], "requested_parameters": ["tools"]})
    add("parameters:pinned_to_a_route_without", {"model": GLM, "requested_parameters": ["tools"], "provider": {"only": ["together"]}})
    add("parameters:two_unsupported", {"model": GLM, "requested_parameters": ["tools", "tool_choice"], "provider": {"only": ["together"]}})
    add("parameters:filters_empty_first", {"model": GLM, "requested_parameters": ["tools"],
                                           "provider": {"only": ["together"], "min_privacy": "confidential"}})
    add("parameters:on_messages", {"model": GLM, "requested_parameters": ["tools"], "route_type": "messages"})
    add("parameters:on_responses", {"model": GLM, "requested_parameters": ["tools"], "route_type": "responses"})
    add("parameters:without_route_type", {"model": GLM, "requested_parameters": ["tools"], "route_type": None})
    add("parameters:spaced_and_blank", {"model": GLM, "requested_parameters": [" tools ", "", "  "]})
    add("parameters:non_capability", {"model": GLM, "requested_parameters": ["logprobs", "temperature"]})
    add("parameters:duplicate", {"model": GLM, "requested_parameters": ["tools", "tools"]})
    # provider.require_parameters: every requested name, or an alias of it
    # (_PARAMETER_CAPABILITY_ALIASES), must be declared by the route, on any
    # route type; false filters nothing; a null or a non-boolean is a 400.
    add("require_parameters:supported", {"model": GLM, "requested_parameters": ["max_tokens"], "provider": {"require_parameters": True}})
    add("require_parameters:alias", {"model": GLM, "requested_parameters": ["max_completion_tokens"], "provider": {"require_parameters": True}})
    add("require_parameters:reasoning_alias", {"model": GLM, "requested_parameters": ["reasoning_effort"], "provider": {"require_parameters": True}})
    add("require_parameters:structured_alias", {"model": GLM, "requested_parameters": ["response_format"], "provider": {"require_parameters": True}})
    add("require_parameters:logprobs", {"model": GLM, "requested_parameters": ["logprobs"], "provider": {"require_parameters": True}})
    add("require_parameters:tools_and_logprobs", {"model": GLM, "requested_parameters": ["tools", "logprobs"], "provider": {"require_parameters": True}})
    add("require_parameters:false", {"model": GLM, "requested_parameters": ["logprobs"], "provider": {"require_parameters": False}})
    add("require_parameters:on_messages", {"model": GLM, "requested_parameters": ["tools"], "route_type": "messages",
                                           "provider": {"require_parameters": True}})
    add("require_parameters:null", {"model": GLM, "provider": {"require_parameters": None}})
    add("require_parameters:not_boolean", {"model": GLM, "provider": {"require_parameters": "true"}})
    add("require_parameters:with_privacy", {"model": GLM, "requested_parameters": ["tools"],
                                            "provider": {"require_parameters": True, "min_privacy": "zdr"}})
    add("require_parameters:privacy_empties", {"model": FLASH, "requested_parameters": ["tools"],
                                               "provider": {"require_parameters": True, "min_privacy": "zdr"}})
    return out


def render() -> str:
    """The file's text: one case a line, so a change shows as the cases it changes."""
    body = (ROOT / SNAPSHOT).read_bytes()
    lines = [
        json.dumps({**case, "expect": evaluate(case)}, sort_keys=True, separators=(",", ":"))
        for case in cases()
    ]
    header = {
        "generator": GENERATOR,
        "snapshot": SNAPSHOT,
        "snapshot_sha256": hashlib.sha256(body).hexdigest(),
        "settings": {"regions": list(REGIONS), "primary_region": PRIMARY_REGION},
        "stage_d_inputs": STAGE_D_INPUTS,
        "rerank": None,
    }
    head = json.dumps(header, sort_keys=True, separators=(",", ":"))[:-1]
    return head + ',\n "cases": [\n  ' + ",\n  ".join(lines) + "\n ]}\n"


if __name__ == "__main__":
    if sys.argv[1:] == ["--print"]:
        sys.stdout.write(render())
    else:
        OUTPUT.write_text(render())
        print(f"wrote {OUTPUT.relative_to(ROOT)}")
