"""Authorize's candidate pipeline and model estimate, lifted out of the gateway route.

Three steps, each a function of what it is handed plus the catalog and the
routing code it calls, with no store, request, or process-local state:

1. ``plan_route``: normalize the routing inputs, classify the route, and
   refuse what the request itself makes impossible.
2. ``resolve_candidates``: the catalog candidates for the route type,
   narrowed to those reachable from the request's gateway region.
3. ``finish_candidates``: BYOK presence, the named decision chain, service
   tier, operator-funded costs, the caller's ``allow_fallbacks``, and the
   largest candidate's estimate (the spend ceiling) and reservation (the
   credit hold, whose priced output ``hold_output_tokens`` bounds for
   catalog routes).

The gateway reads what these need (the BYOK rows, the clock, the custom or
user model, the partner mode) and passes it in; it applies the process-local
health re-rank through ``finish_candidates``'s ``rerank`` argument, which the
golden cases and the differential leave absent. Errors are the gateway's
errors, raised unchanged; the two named-chain refusals are raised wrapped in
``NamedChainRefusal`` so the gateway can log them with its request context.

Clocks: ``at`` prices the estimate. The routing functions this calls still
read the process clocks they read today (the lifecycle clock for retirements
and scheduled prices in ``endpoints_for_model``, the catalog clock for
manifest freshness, the wall clock for rank-evidence age); threading those
through routing is a separate change.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import HTTPException

from trusted_router import catalog as model_catalog
from trusted_router.catalog import (
    DECIDE_PATH,
    MODELS,
    NAMED_DECISION_MODEL_PROVIDERS,
    Model,
    ModelEndpoint,
    effective_endpoint,
)
from trusted_router.config import Settings
from trusted_router.custom_model_billing import custom_model_cost_microdollars
from trusted_router.errors import api_error
from trusted_router.openai_service_tiers import (
    OPENAI_PRIORITY_MAX_PROMPT_TOKENS,
    OPENAI_SERVICE_TIERS,
    openai_priority_cost_microdollars,
    openai_priority_pricing,
)
from trusted_router.partner_billing import PartnerBillingMode, partner_cost_microdollars
from trusted_router.provider_contracts import (
    EXPLICIT_GATEWAY_REGIONS_BY_PROVIDER_MODEL,
    provider_model_available_from_gateway_region,
)
from trusted_router.routing import (
    NormalizedRoutingInputs,
    RoutePreferences,
    _apply_endpoint_provider_filters,
    candidates_fitting_context,
    chat_route_endpoint_candidates,
    decide_route_endpoint_candidates,
    embeddings_route_endpoint_candidates,
    fetch_route_endpoint_candidates,
    image_route_endpoint_candidates,
    normalize_routing_inputs,
    speech_route_endpoint_candidates,
    transcription_route_endpoint_candidates,
    video_route_endpoint_candidates,
)
from trusted_router.stage_d import (
    endpoint_cost_microdollars_from_document as _stage_d_document_cost,
)
from trusted_router.stage_d import endpoint_pricing_document
from trusted_router.types import ErrorType, UsageType
from trusted_router.web_fetch import FETCH_ROUTE_TYPE

Candidate = tuple[Model, ModelEndpoint]
Rerank = Callable[[list[Candidate], RoutePreferences], list[Candidate]]

# Route types whose hosted tool or media cost may be reserved beside the model.
ADDITIONAL_COST_ROUTE_TYPES = frozenset(
    {
        "responses.web_search.planner",
        "chat.completions.web_search.planner",
        "images",
        "audio.speech",
        "audio.transcriptions",
        "videos",
    }
)


class NamedChainRefusal(Exception):
    """A named decision chain left no candidate; ``event`` names the log line.

    ``filtered`` is the request's own filters excluding every pinned host
    (400), ``unavailable`` an outage of every permitted host (503). The
    gateway logs it with its request context and raises ``error`` unchanged.
    """

    def __init__(self, event: str, error: HTTPException) -> None:
        super().__init__(event)
        self.event = event
        self.error = error


@dataclass(frozen=True)
class CandidateRequest:
    """The parsed request fields candidate selection reads.

    ``routing_body`` is the routing view of the request body exactly as the
    gateway hands it to ``normalize_routing_inputs`` (model, models, provider,
    requested parameters, input modalities, route type, region, service tier),
    after a custom model's base model and forced Credits are applied.
    """

    route_type: str | None
    route_model_id: str
    routing_body: dict[str, Any]
    body_region: str | None
    video_resolution: str | None = None
    video_allowed_providers: frozenset[str] | None = None
    custom_model: bool = False


@dataclass(frozen=True)
class RoutePlan:
    route_model_id: str
    normalized: NormalizedRoutingInputs
    requested_model: Model | None
    named_decision_chain: tuple[str, ...] | None
    supports_live_routing: bool
    is_video: bool
    is_image: bool
    is_speech: bool
    is_transcription: bool
    is_fetch: bool
    is_embeddings: bool
    is_decide: bool

    @property
    def preferences(self) -> RoutePreferences:
        return self.normalized.preferences


@dataclass(frozen=True)
class ResolvedCandidates:
    candidates: list[Candidate]
    effective_preferences: RoutePreferences


@dataclass(frozen=True)
class CandidateOutcome:
    """What authorize derives from the candidates: their order, the selected
    (first) candidate, the normalized service tier, the model estimate (the
    spend ceiling: the priciest candidate at the whole output budget) and the
    model reservation (the credit hold: the same at ``hold_output_tokens``
    for a non-stream catalog route; equal to the estimate for streams, for
    now, and for user-provided, partner and video routes)."""

    candidates: list[Candidate]
    service_tier: str | None
    model_estimate: int
    model_reservation: int

    @property
    def model(self) -> Model:
        return self.candidates[0][0]

    @property
    def endpoint(self) -> ModelEndpoint:
        return self.candidates[0][1]


def plan_route(request: CandidateRequest, settings: Settings, *, region: str) -> RoutePlan:
    """Normalize routing and refuse what the request makes impossible."""
    route_type = request.route_type
    route_model_id = request.route_model_id
    is_video = route_type == "videos"
    is_image = route_type == "images"
    normalized = normalize_routing_inputs(
        request.routing_body,
        settings,
        resolved_region=region,
    )
    route_preferences = normalized.preferences
    requested_model = MODELS.get(route_model_id) if route_model_id else None
    is_embeddings = (
        requested_model is not None
        and requested_model.supports_embeddings
        and not requested_model.supports_chat
    )
    # Hosted decision models dispatch on the MODEL, like embeddings: a native
    # decision request names an ordinary chat model and takes the chat arm.
    is_decide = (
        requested_model is not None
        and requested_model.supports_decide
        and not requested_model.supports_chat
    )
    named_decision_chain = NAMED_DECISION_MODEL_PROVIDERS.get(route_model_id or "")
    supports_live_routing = (
        route_type in {"chat.completions", "responses", "messages"}
        and named_decision_chain is None
        and not (is_image or is_video or is_embeddings or is_decide)
    )
    if not supports_live_routing and (
        route_preferences.preferred_max_latency or route_preferences.preferred_min_throughput
    ):
        raise api_error(
            501,
            "Performance preferences currently require a synchronous text-generation endpoint",
            "not_supported_in_alpha",
        )
    if (is_decide or named_decision_chain is not None) and route_type != "decide":
        # A decision model has no chat surface. Jev would fail at the provider,
        # and a named model like trev-1.0 would silently become a plain chat
        # alias for its backing model on whichever host is cheapest -- not the
        # thing the name promises. Say so instead.
        raise api_error(
            400,
            f"{route_model_id} is a decision model: call POST {DECIDE_PATH}",
            ErrorType.MODEL_NOT_SUPPORTED,
        )
    if named_decision_chain is not None:
        # A caller excluding the entire pinned chain is an invalid request,
        # not an outage. Check policy before live regional availability so a
        # genuinely unavailable permitted host still returns retryable 503.
        permitted_hosts = set(named_decision_chain) - route_preferences.ignore
        if route_preferences.only:
            permitted_hosts.intersection_update(route_preferences.only)
        if not permitted_hosts:
            raise api_error(
                400,
                f"Provider filters exclude every supported host for {route_model_id}",
                ErrorType.BAD_REQUEST,
            )
    return RoutePlan(
        route_model_id=route_model_id,
        normalized=normalized,
        requested_model=requested_model,
        named_decision_chain=named_decision_chain,
        supports_live_routing=supports_live_routing,
        is_video=is_video,
        is_image=is_image,
        is_speech=route_type == "audio.speech",
        is_transcription=route_type == "audio.transcriptions",
        is_fetch=route_type == FETCH_ROUTE_TYPE,
        is_embeddings=is_embeddings,
        is_decide=is_decide,
    )


def resolve_candidates(
    plan: RoutePlan,
    request: CandidateRequest,
    *,
    region: str,
    at: datetime,
    user_model_candidate: Candidate | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> ResolvedCandidates:
    """The route type's candidates, reachable from the request's gateway region.

    A user-provided model's owner-dispatch candidate is built by the gateway
    (after its own checks) and passed in; it skips the catalog resolvers.
    """
    normalized = plan.normalized
    effective_preferences = plan.preferences
    endpoint_candidates: Sequence[Candidate]
    if user_model_candidate is not None:
        endpoint_candidates = [user_model_candidate]
    elif plan.is_video:
        endpoint_candidates = video_route_endpoint_candidates(
            normalized,
            defer_no_fallback_selection=True,
            video_resolution=request.video_resolution,
            pricing_effective_at=at,
            allowed_providers=request.video_allowed_providers,
        )
    elif plan.is_fetch:
        endpoint_candidates = fetch_route_endpoint_candidates(normalized)
    elif plan.is_transcription:
        if request.custom_model:
            raise api_error(400, "Custom models do not support transcription", ErrorType.MODEL_NOT_SUPPORTED)
        endpoint_candidates = transcription_route_endpoint_candidates(normalized)
    elif plan.is_speech:
        if request.custom_model:
            raise api_error(400, "Custom models do not support speech", ErrorType.MODEL_NOT_SUPPORTED)
        endpoint_candidates = speech_route_endpoint_candidates(normalized)
    elif plan.is_image:
        if request.custom_model:
            raise api_error(
                400,
                "Custom models do not support image generation",
                ErrorType.MODEL_NOT_SUPPORTED,
            )
        endpoint_candidates = image_route_endpoint_candidates(
            normalized,
            defer_no_fallback_selection=True,
        )
    elif plan.is_decide:
        resolved = decide_route_endpoint_candidates(
            normalized,
            defer_no_fallback_selection=True,
        )
        effective_preferences = resolved.effective_preferences
        endpoint_candidates = resolved
    elif plan.is_embeddings:
        endpoint_candidates = embeddings_route_endpoint_candidates(
            normalized,
            defer_no_fallback_selection=True,
        )
        if not endpoint_candidates:
            raise api_error(400, "Model does not support embeddings", ErrorType.MODEL_NOT_SUPPORTED)
    else:
        resolved = chat_route_endpoint_candidates(
            normalized,
            defer_no_fallback_selection=True,
        )
        effective_preferences = resolved.effective_preferences
        endpoint_candidates = candidates_fitting_context(
            resolved, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        )
        if not endpoint_candidates:
            raise api_error(
                400, "Model does not support chat completions", ErrorType.MODEL_NOT_SUPPORTED
            )
    candidates = [
        (candidate_model, candidate_endpoint)
        for candidate_model, candidate_endpoint in endpoint_candidates
        if provider_model_available_from_gateway_region(
            candidate_endpoint.provider,
            candidate_endpoint.model_id,
            # New native adapters need an explicit, upgraded gateway origin;
            # missing metadata must not inherit the control plane's region.
            # Preserve legacy region fallback for already-deployed providers.
            request.body_region or (
                "" if candidate_endpoint.provider in {"amazon-bedrock", "maple"}
                or (candidate_endpoint.provider, candidate_endpoint.model_id)
                in EXPLICIT_GATEWAY_REGIONS_BY_PROVIDER_MODEL else region
            ),
        )
    ]
    return ResolvedCandidates(candidates, effective_preferences)


def finish_candidates(
    plan: RoutePlan,
    resolved: ResolvedCandidates,
    *,
    route_type: str | None,
    byok_providers: frozenset[str],
    estimated_input_tokens: int,
    output_tokens: int,
    service_tier: str | None,
    additional_cost_reservation: int,
    video_resolution: str | None,
    at: datetime,
    user_model_prices: tuple[int, int] | None = None,
    partner_mode: PartnerBillingMode | None = None,
    rerank: Rerank | None = None,
    stream: bool = False,
) -> CandidateOutcome:
    """Admit, order and price the candidates.

    ``stream`` is the request's stream flag: a stream's reservation is its
    estimate for now (see ``hold_output_tokens``).

    ``byok_providers`` are the provider slugs whose BYOK credentials the
    workspace holds; a BYOK candidate of any other provider is dropped.
    ``estimated_input_tokens`` already includes a custom model's hidden prompt.
    ``rerank`` is the process-local health re-rank, applied to live-routing
    requests before ``allow_fallbacks`` keeps the first candidate; ``None``
    (as in the golden cases) applies none.
    """
    endpoint_candidates = [
        (model, endpoint)
        for model, endpoint in resolved.candidates
        if not UsageType.for_endpoint(endpoint).is_byok() or endpoint.provider in byok_providers
    ]
    named_decision_chain = plan.named_decision_chain
    if named_decision_chain is not None:
        endpoint_candidates = _named_chain_candidates(
            plan, resolved.effective_preferences, endpoint_candidates, named_decision_chain,
        )
    input_tokens = estimated_input_tokens
    service_tier = requested_service_tier_or_error(service_tier)
    endpoint_candidates = service_tier_endpoint_candidates_or_error(
        endpoint_candidates,
        service_tier=service_tier,
        estimated_input_tokens=input_tokens,
    )
    if additional_cost_reservation:
        if route_type not in ADDITIONAL_COST_ROUTE_TYPES:
            raise api_error(
                400,
                "additional cost reservations require a supported hosted tool or media route",
                ErrorType.BAD_REQUEST,
            )
        # Hosted tools and asynchronous media are operator-funded, so their
        # fixed cost must settle against Credits rather than a BYOK route.
        endpoint_candidates = [
            (candidate_model, candidate_endpoint)
            for candidate_model, candidate_endpoint in endpoint_candidates
            if UsageType.for_endpoint(candidate_endpoint) == UsageType.CREDITS
        ]
    if plan.supports_live_routing and rerank is not None:
        endpoint_candidates = rerank(endpoint_candidates, resolved.effective_preferences)
    # ``allow_fallbacks=false`` removes alternate models in the resolver, but
    # provider selection must happen after regional, workspace/BYOK, and
    # service-tier eligibility. Truncating the raw catalog first can pin an
    # unusable endpoint and reject a model another eligible provider serves.
    if not plan.preferences.allow_fallbacks:
        endpoint_candidates = endpoint_candidates[:1]
    if not endpoint_candidates:
        raise api_error(
            400,
            "No authorized route candidates are available for this workspace",
            ErrorType.PROVIDER_NOT_SUPPORTED,
        )
    if user_model_prices is not None:
        model_estimate = custom_model_cost_microdollars(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            prompt_price=user_model_prices[0],
            completion_price=user_model_prices[1],
        )
        model_reservation = model_estimate
    elif partner_mode is not None:
        model_estimate = partner_cost_microdollars(
            partner_mode,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        model_reservation = model_estimate
    elif video_resolution is not None:
        model_estimate = max(
            endpoint_cost_microdollars_from_document(
                endpoint_pricing_document((candidate_endpoint,)),
                candidate_endpoint.id,
                input_tokens,
                output_tokens,
            )
            for _candidate_model, candidate_endpoint in endpoint_candidates
        )
        model_reservation = model_estimate
    else:
        def priciest(priced_output_tokens: int) -> int:
            return max(
                endpoint_cost_microdollars(
                    candidate_endpoint,
                    input_tokens,
                    priced_output_tokens,
                    effective_at=at,
                    service_tier=service_tier,
                    reserve_auto=True,
                )
                for _candidate_model, candidate_endpoint in endpoint_candidates
            )

        model_estimate = priciest(output_tokens)
        # A video's output tokens are video tokens and its settle is held to
        # the ceiling: its reservation is the ceiling. A stream's reservation
        # is the ceiling too, for now: the Stage D heartbeat reader deployed
        # before this change bounds a stream's running charge by the
        # reservation row's credit hold, so a mixed-version deploy or a
        # rollback would cut a stream admitted with a lower reservation at
        # usage_exceeds_cap. Follow-up: lower stream reservations once every
        # plane runs the reader that caps at the estimate
        # (docs/design/fast-admission-e2-decide.md, "The two figures").
        reserved_output_tokens = (
            output_tokens if route_type == "videos" or stream else hold_output_tokens(output_tokens)
        )
        model_reservation = (
            model_estimate if reserved_output_tokens == output_tokens else priciest(reserved_output_tokens)
        )
    return CandidateOutcome(endpoint_candidates, service_tier, model_estimate, model_reservation)


# The output tokens a catalog route's credit reservation prices at most. The
# Go edge computes the same reservation from the routing snapshot with its
# own copy, HoldOutputTokens in fastpath/internal/routingsnapshot/evaluate.go:
# the two must agree, or the two paths reserve different amounts for one
# request.
HOLD_OUTPUT_TOKENS = 16_384


def hold_output_tokens(output_tokens: int) -> int:
    """The output tokens a catalog route's credit reservation prices:
    ``min(the client's budget, HOLD_OUTPUT_TOKENS)``.

    An authorization carries two figures. The ESTIMATE (``model_estimate``,
    the authorization row's ``estimated_microdollars``, the 200's
    ``estimated_cost``) is the spend ceiling: the priciest candidate's price
    of the prompt plus the client's whole output budget (``max_tokens``,
    ``max_completion_tokens`` or ``max_output_tokens``, else 512). Stage D
    exports it as ``cap_micro`` and cuts a stream whose running charge passes
    it, the heartbeat refuses usage above it, a video's settle and a
    user-provided model's settle are held to it, and the Go reaper charges an
    abandoned hold at most its hold. Key admission (the lifetime cap, the
    spend windows, the strict windows) compares it too, and the reservation
    row's ``key_reserved_micro`` records it: a key cap counts outstanding
    spend ceilings. The RESERVATION (``model_reservation``, the reservation
    row's ``credit_reserved_micro``, the lease hold on the fast path) is what
    workspace credit admission compares with the available balance and what
    the credit counters hold until settle: the same price at the output
    tokens this function bounds. A stream's reservation is its estimate for
    now (``finish_candidates``, ``stream``).

    The budget is a ceiling, not a forecast: a client sending the model's
    whole output window as ``max_tokens`` held the window's worst case per
    request, and a few in-flight requests could exhaust a balance that
    covered what they would actually cost. Only the reservation's priced
    output is bounded; the context window check is unchanged (it counts
    ``max_tokens``, else ``max_completion_tokens``, never
    ``max_output_tokens``).

    A catalog text route's settle is capped at neither figure (a video's, a
    user-provided model's and Polyphemus's settle are held to the estimate,
    which is also their reservation), and nothing in authorize bounds the
    charge (cache-creation premiums and counts above the budget settle in
    full), so the uncovered cost of an in-flight request,
    ``max(0, actual minus reservation)``, has no bound of its own; against
    the single hold at the estimate it was ``max(0, actual minus estimate)``,
    so the bound here raises it by at most the estimate minus the
    reservation.
    """
    return min(output_tokens, HOLD_OUTPUT_TOKENS)


def _named_chain_candidates(
    plan: RoutePlan,
    effective_preferences: RoutePreferences,
    endpoint_candidates: list[Candidate],
    named_decision_chain: tuple[str, ...],
) -> list[Candidate]:
    # The chain is part of what the name means (and of its advertised
    # price), so it is enforced HERE, not merely requested by the gateway:
    # only the pinned hosts, in the pinned order, whatever the request's
    # provider preferences say.
    chain_rank = {provider: rank for rank, provider in enumerate(named_decision_chain)}
    chained = sorted(
        (
            candidate
            for candidate in endpoint_candidates
            if candidate[1].provider in chain_rank and not candidate[1].is_byok
        ),
        key=lambda candidate: chain_rank[candidate[1].provider],
    )
    if chained:
        return chained
    route_model_id = plan.route_model_id
    # Establish request compatibility independently of availability.
    # endpoints_for_model drops expired/retired routes, so use the
    # catalog entries (at effective prices) before those exclusions.
    # An available but request-excluded host says nothing about a
    # compatible host's outage. BYOK is never part of a named chain.
    pinned_credit_candidates = [
        (MODELS[candidate_endpoint.model_id], effective_endpoint(candidate_endpoint))
        for candidate_endpoint in model_catalog.MODEL_ENDPOINTS.values()
        if candidate_endpoint.model_id in plan.normalized.model_ids
        and candidate_endpoint.provider in chain_rank
        and not candidate_endpoint.is_byok
    ]
    # Reuse relaxation over the resolver's full backing candidates;
    # recomputing it over just pinned hosts would broaden the policy.
    if pinned_credit_candidates and not _apply_endpoint_provider_filters(
        pinned_credit_candidates, effective_preferences
    ):
        raise NamedChainRefusal("filtered", api_error(
            400,
            f"No host in the {route_model_id} chain matches the request's routing "
            "filters; the chain's hosts are fixed, so relax the usage, privacy, "
            "jurisdiction or provider filters",
            ErrorType.BAD_REQUEST,
        ))
    raise NamedChainRefusal("unavailable", api_error(
        503,
        f"No host in the {route_model_id} chain is available; retry shortly",
        ErrorType.SERVICE_UNAVAILABLE,
        headers={"Retry-After": "2"},
    ))


def requested_service_tier_or_error(service_tier: str | None) -> str | None:
    if service_tier is None:
        return None
    normalized = service_tier.strip().lower()
    if normalized not in OPENAI_SERVICE_TIERS:
        raise api_error(
            400,
            "service_tier must be default, auto, or priority",
            ErrorType.BAD_REQUEST,
        )
    return normalized


def service_tier_endpoint_candidates_or_error(
    candidates: list[Candidate],
    *,
    service_tier: str | None,
    estimated_input_tokens: int,
) -> list[Candidate]:
    if service_tier is None:
        return candidates
    openai_candidates = [
        (model, endpoint) for model, endpoint in candidates if endpoint.provider == "openai"
    ]
    if service_tier in {"auto", "priority"}:
        openai_candidates = [
            (model, endpoint)
            for model, endpoint in openai_candidates
            if openai_priority_pricing(endpoint.model_id) is not None
        ]
    if service_tier == "priority" and estimated_input_tokens > OPENAI_PRIORITY_MAX_PROMPT_TOKENS:
        raise api_error(
            400,
            "OpenAI Priority processing does not support prompts over 272000 tokens",
            ErrorType.BAD_REQUEST,
        )
    if not openai_candidates:
        raise api_error(
            400,
            f"OpenAI {service_tier} processing is unavailable for the requested model",
            ErrorType.MODEL_NOT_SUPPORTED,
        )
    return openai_candidates


def endpoint_cost_microdollars(
    endpoint: ModelEndpoint,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    price_tier_input_tokens: int | None = None,
    effective_at: datetime | str | None = None,
    service_tier: str | None = None,
    reserve_auto: bool = False,
) -> int:
    """input_tokens must be the UNCACHED prompt tokens when cache counts
    are passed — cached reads/writes bill at the provider-specific
    multiple of the prompt price (see catalog.cache_token_prices_microdollars)."""
    endpoint = effective_endpoint(endpoint, at=effective_at)
    # A reseller may echo its upstream's tier; it does not change the
    # reseller's authorized catalog tariff. Only direct OpenAI routes use
    # OpenAI Priority prices, regardless of the publisher in model_id.
    if endpoint.provider == "openai" and (
        service_tier == "priority" or (service_tier == "auto" and reserve_auto)
    ):
        return openai_priority_cost_microdollars(
            endpoint.model_id,
            input_tokens,
            output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
        )
    document = endpoint_pricing_document((endpoint,))
    return endpoint_cost_microdollars_from_document(
        document,
        endpoint.id,
        input_tokens,
        output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        price_tier_input_tokens=price_tier_input_tokens,
    )


def endpoint_cost_microdollars_from_document(
    document: dict[str, Any],
    endpoint_id: str,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    price_tier_input_tokens: int | None = None,
) -> int:
    """The Stage D cost path, with the frozen document as its sole price input."""
    return _stage_d_document_cost(
        document,
        endpoint_id,
        input_tokens,
        output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        price_tier_input_tokens=price_tier_input_tokens,
    )
