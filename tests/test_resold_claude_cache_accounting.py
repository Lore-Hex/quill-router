"""Cache accounting for Claude resold by amazon-bedrock and google-vertex.

The enclave relays Anthropic-native usage, whose input count EXCLUDES the
cached tokens, for exactly two resold paths (enclave-go/internal/llm/multi.go):
amazon-bedrock when the upstream model id is under ``us.anthropic.`` and
google-vertex when the direct model id is under ``claude-``. The settle body
carries no convention field, and the router's rule once read the provider
slug alone, so for those endpoints the real path computed
``uncached = max(input - cached, 0)`` and recorded ``total = input``: an
under-bill of the uncached input and an understated prompt total whenever
caching was used.

The rule every site reads now (provider_contracts.provider_input_excludes_cache)
is keyed on the provider slug and the router model id, the two fields the
settle path, the outbox repair path, the async settle boundary, the frozen
billing snapshot, the fast path edge and Stage D's delivered-usage billing
(the heartbeat's running charge and cap check, the reaper's snapshot booking
and its generation) all have:

    anthropic, or (amazon-bedrock or google-vertex) serving an anthropic/ model.

These tests tie the rule to the enclave's dispatch conditions over the whole
catalog, and hold the real settle path to the corrected amounts.
"""
# ruff: noqa: F811, F401 - env is the shared fixture of tests/test_async_settle_handler.py
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.fakes.spanner import FakeSpannerDatabase, _ParamTypes
from tests.fixture_routes import drop_routes, serve_on_fixture_route
from tests.test_async_settle_handler import env, prepare
from tests.test_async_settle_proof import catalog, legacy_body
from tests.test_billing_snapshot import CASES
from tests.test_settle_outbox_drain import _client
from trusted_router import billing_snapshot as billing
from trusted_router.catalog import endpoint_for_id
from trusted_router.catalog_data import ModelEndpoint, PriceTier
from trusted_router.catalog_registry import MODEL_ENDPOINTS
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.provider_contracts import (
    BEDROCK_RUNTIME_PROFILES,
    CACHE_EXCLUSIVE_INPUT_PROVIDERS,
    CACHE_EXCLUSIVE_INPUT_PUBLISHER_PREFIX,
    CACHE_EXCLUSIVE_INPUT_RESELLERS,
    provider_input_excludes_cache,
)
from trusted_router.routes.internal import gateway
from trusted_router.services.settle_outbox_apply import normalized_prompt_accounting
from trusted_router.stage_d import (
    canonical_pricing_snapshot,
    endpoint_pricing_document,
    parse_pricing_snapshot,
)
from trusted_router.storage import STORE
from trusted_router.storage_gcp_authorize import (
    reap_expired_reservations_result,
    snapshot_generation,
)
from trusted_router.storage_gcp_counter_dml import insert_reservation
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE, KEY_LIMIT_TABLE
from trusted_router.storage_gcp_request_records import (
    insert_gateway_authorization,
    read_gateway_authorization,
)
from trusted_router.storage_gcp_stage_d import (
    HeartbeatResult,
    delivered_usage_charge_microdollars,
    delivered_usage_cost_microdollars,
    heartbeat_gateway_atomic,
    normalized_delivered_prompt,
)
from trusted_router.storage_models import GatewayAuthorization
from trusted_router.types import UsageType

BEDROCK_CLAUDE = "anthropic/claude-sonnet-5"
VERTEX_CLAUDE = "anthropic/claude-sonnet-4.6"
BEDROCK_OTHER = "openai/gpt-oss-120b"
DIRECT_CLAUDE = "anthropic/claude-haiku-4.5"


def _claude_wire_name(name: str | None) -> bool:
    """azure.go's azureUsesAnthropicWire over one name: folded, trimmed, the
    segment after the last slash under claude-."""
    folded = (name or "").strip().lower()
    return folded.rsplit("/", 1)[-1].startswith("claude-")


def _enclave_relays_anthropic_native_usage(provider: str, upstream_id: str | None, model_id: str) -> bool:
    """The enclave's dispatch conditions for an Anthropic-native relay, as
    enclave-go/internal/llm reads them: the anthropic provider itself; the
    Bedrock runtime for an upstream id under us.anthropic. (multi.go); the
    public Vertex provider's Claude path for a direct model id under claude-
    (multi.go); Azure Foundry's Anthropic client for an upstream or requested
    model id whose last segment is under claude- (azure.go,
    azureUsesAnthropicWire); the legacy "vertex" slug's gcpClient
    (multi.go); and zero-g for an upstream id under claude- or a model id
    under anthropic/ (zero_g.go, zeroGUsesAnthropicFormat). The shared rule
    covers the first three; the catalog has no azure anthropic/ endpoint, no
    "vertex" provider and no zero-g Claude endpoint today (zero-g serves
    z-ai/ and its own models), so the equivalence below holds, and the day
    such an endpoint appears this test fails and the rule must grow on both
    sides (router and enclave) before it ships."""
    upstream = upstream_id or ""
    folded_upstream = upstream.strip().lower()
    return (
        provider == "anthropic"
        or (provider == "amazon-bedrock" and upstream.startswith("us.anthropic."))
        or (provider == "google-vertex" and upstream.startswith("claude-"))
        or (provider == "azure" and (_claude_wire_name(upstream_id) or _claude_wire_name(model_id)))
        or provider == "vertex"
        # enclave-go/internal/llm/zero_g.go, zeroGUsesAnthropicFormat
        or (provider == "zero-g" and (folded_upstream.startswith("claude-") or model_id.lower().startswith("anthropic/")))
    )


_BEDROCK_MANIFEST = Path(__file__).resolve().parents[1] / "src/trusted_router/data/provider_models/amazon-bedrock.json"


def _bedrock_manifest_rows() -> list[dict[str, Any]]:
    rows = json.loads(_BEDROCK_MANIFEST.read_text(encoding="utf-8"))["models"]
    return [row for row in rows if isinstance(row, dict)]


def _bedrock_runtime_table_disagreements(
    endpoints: Any, manifest_rows: list[dict[str, Any]],
) -> list[str]:
    """BEDROCK_RUNTIME_PROFILES against the Bedrock manifest and the catalog.

    The manifest keeps every reviewed Runtime row, routable or withheld, so
    its anthropic/ rows under us.anthropic. equal the table exactly. The
    hourly refresh probes each profile once and withholds a route whose
    canary fails (provider-canary-failed) until a later refresh passes it, so
    the catalog's amazon-bedrock Claude endpoints equal exactly the profiles
    whose manifest row is routable: never a profile the table lacks, never a
    different upstream id, never a withheld route."""
    problems: list[str] = []
    reviewed = {row["id"]: row for row in manifest_rows
                if str(row.get("id", "")).startswith("anthropic/")
                and str(row.get("upstream_id", "")).startswith("us.anthropic.")}
    manifest_table = {model: row["upstream_id"] for model, row in reviewed.items()}
    if manifest_table != BEDROCK_RUNTIME_PROFILES:
        problems.append(f"manifest Runtime rows {manifest_table} != table {BEDROCK_RUNTIME_PROFILES}")
    available = {model: profile for model, profile in BEDROCK_RUNTIME_PROFILES.items()
                 if reviewed.get(model, {}).get("routable") is not False}
    published = {e.model_id: e.upstream_id for e in endpoints
                 if e.provider == "amazon-bedrock" and provider_input_excludes_cache(e.provider, e.model_id)}
    if published != available:
        problems.append(f"catalog Bedrock Claude {published} != canary-available profiles {available}")
    for model, profile in BEDROCK_RUNTIME_PROFILES.items():
        if not (provider_input_excludes_cache("amazon-bedrock", model)
                and _enclave_relays_anthropic_native_usage("amazon-bedrock", profile, model)):
            problems.append(f"rule and dispatch disagree on table entry {model} -> {profile}")
    return problems


def test_bedrock_runtime_table_check_follows_the_hourly_canary() -> None:
    """A failed canary withholds one profile's route: the table still holds,
    and still fails on a real mismatch (wrong profile, route the table lacks,
    a withheld route that is published, a reviewed row dropped)."""
    endpoint = SimpleNamespace
    rows = [{"id": model, "upstream_id": profile, "routable": True}
            for model, profile in BEDROCK_RUNTIME_PROFILES.items()]
    every = [endpoint(provider="amazon-bedrock", model_id=model, upstream_id=profile)
             for model, profile in BEDROCK_RUNTIME_PROFILES.items()]
    assert not _bedrock_runtime_table_disagreements(every, rows)

    withheld_model = next(iter(BEDROCK_RUNTIME_PROFILES))
    withheld_rows = [dict(row, routable=False, routable_reason="provider-canary-failed")
                     if row["id"] == withheld_model else row for row in rows]
    rest = [e for e in every if e.model_id != withheld_model]
    assert not _bedrock_runtime_table_disagreements(rest, withheld_rows)
    # The withheld route must not be published.
    assert _bedrock_runtime_table_disagreements(every, withheld_rows)
    # A routable row missing from the catalog is a real disagreement.
    assert _bedrock_runtime_table_disagreements(rest, rows)
    # A published profile that differs from the table.
    wrong = [endpoint(provider=e.provider, model_id=e.model_id, upstream_id=e.upstream_id + "-v2")
             if e.model_id == withheld_model else e for e in every]
    assert _bedrock_runtime_table_disagreements(wrong, rows)
    # A Bedrock Claude route the table lacks.
    extra = [*every, endpoint(provider="amazon-bedrock", model_id="anthropic/claude-new",
                              upstream_id="us.anthropic.claude-new")]
    assert _bedrock_runtime_table_disagreements(extra, rows)
    # A reviewed row the manifest dropped, even with the route withheld.
    assert _bedrock_runtime_table_disagreements(rest, [r for r in rows if r["id"] != withheld_model])


def test_the_rule_is_the_enclaves_dispatch_over_the_whole_catalog() -> None:
    """For every endpoint of the catalog, every usage type included, the
    rule over (provider, model_id) answers exactly as the enclave's dispatch
    over (provider, upstream id) does. A future endpoint that breaks the
    equivalence (a Bedrock Claude id outside us.anthropic., a Vertex
    anthropic/ model outside claude-, a third reseller relaying native
    usage) fails here, naming it."""
    disagreements = []
    by_kind = {"anthropic": 0, "amazon-bedrock": 0, "google-vertex": 0, "other_anthropic_publisher": 0}
    for endpoint in MODEL_ENDPOINTS.values():
        rule = provider_input_excludes_cache(endpoint.provider, endpoint.model_id)
        dispatch = _enclave_relays_anthropic_native_usage(endpoint.provider, endpoint.upstream_id, endpoint.model_id)
        if rule != dispatch:
            disagreements.append((endpoint.id, endpoint.upstream_id, rule, dispatch))
        if rule:
            by_kind[endpoint.provider] += 1
        elif endpoint.model_id.startswith(CACHE_EXCLUSIVE_INPUT_PUBLISHER_PREFIX):
            by_kind["other_anthropic_publisher"] += 1
    assert not disagreements, disagreements
    # The router's own table of Bedrock runtime profiles (the us.anthropic.
    # ids the Bedrock runtime serves) is exactly the provider's reviewed
    # Claude rows, and the catalog publishes exactly the ones whose hourly
    # canary passed, so the rule and that table agree too.
    assert not _bedrock_runtime_table_disagreements(MODEL_ENDPOINTS.values(), _bedrock_manifest_rows())
    # The equivalence is over a catalog that reaches every branch of it.
    assert by_kind["anthropic"] >= 1 and by_kind["amazon-bedrock"] >= 1 and by_kind["google-vertex"] >= 1, by_kind
    # Other providers serving an anthropic/ model (deepinfra's BYOK routes
    # today) relay OpenAI-style counts and stay inclusive.
    assert by_kind["other_anthropic_publisher"] >= 1, by_kind
    # The three relays the rule does not cover are absent from the catalog.
    assert not any(e.provider == "vertex" for e in MODEL_ENDPOINTS.values())
    assert not any(e.provider == "azure" and (_claude_wire_name(e.upstream_id) or _claude_wire_name(e.model_id))
                   for e in MODEL_ENDPOINTS.values())
    assert not any(e.provider == "zero-g" and ((e.upstream_id or "").strip().lower().startswith("claude-")
                                               or e.model_id.lower().startswith("anthropic/"))
                   for e in MODEL_ENDPOINTS.values())


def test_the_dispatch_stand_in_reads_the_enclaves_conditions() -> None:
    """The stand-in's own branches, each reached, so a disagreement above
    names the right side."""
    relays = _enclave_relays_anthropic_native_usage
    assert relays("anthropic", None, "anthropic/claude-x")
    assert relays("anthropic", "claude-x", "openai/x")
    assert relays("amazon-bedrock", "us.anthropic.claude-sonnet-5", "anthropic/claude-sonnet-5")
    assert not relays("amazon-bedrock", "anthropic.claude-sonnet-5", "anthropic/claude-sonnet-5")
    assert not relays("amazon-bedrock", "openai.gpt-oss-120b", "openai/gpt-oss-120b")
    assert relays("google-vertex", "claude-sonnet-4-6", "anthropic/claude-sonnet-4.6")
    assert not relays("google-vertex", "gemini-2.5-pro", "google/gemini-2.5-pro")
    assert relays("azure", "claude-opus-5", "anthropic/claude-opus-5")
    assert relays("azure", None, "anthropic/claude-opus-5")
    assert relays("azure", " Anthropic/Claude-Opus-5 ", "x")
    assert not relays("azure", "gpt-5-4-mini", "openai/gpt-5.4-mini")
    assert relays("vertex", "gemini-2.5-pro", "google/gemini-2.5-pro")
    assert relays("zero-g", "claude-opus-5", "anthropic/claude-opus-5")
    assert relays("zero-g", " Claude-Opus-5 ", "zero-g/alias")
    assert relays("zero-g", "opus-5", "Anthropic/claude-opus-5")
    assert not relays("zero-g", "glm-5.3", "z-ai/glm-5.3")
    assert not relays("zero-g", "0gm-1.0-35b-a3b", "zero-g/0gm-1.0-35b-a3b")
    assert not relays("deepinfra", "anthropic/claude-opus-5", "anthropic/claude-opus-5")
    assert not relays("together", None, "anthropic/claude-x")


def test_the_rule_over_its_two_keys() -> None:
    assert CACHE_EXCLUSIVE_INPUT_PROVIDERS == {"anthropic"}
    assert CACHE_EXCLUSIVE_INPUT_RESELLERS == {"amazon-bedrock", "google-vertex"}
    assert CACHE_EXCLUSIVE_INPUT_PUBLISHER_PREFIX == "anthropic/"
    for provider in CACHE_EXCLUSIVE_INPUT_RESELLERS:
        assert provider_input_excludes_cache(provider, "anthropic/claude-x") is True
        assert provider_input_excludes_cache(provider, "openai/gpt-oss-x") is False
        assert provider_input_excludes_cache(provider, "anthropic") is False
        # The slug alone answers the slug's: inclusive.
        assert provider_input_excludes_cache(provider) is False
        assert provider_input_excludes_cache(provider, None) is False
    assert provider_input_excludes_cache("anthropic") is True
    assert provider_input_excludes_cache("anthropic", "anthropic/claude-x") is True
    assert provider_input_excludes_cache("anthropic", "openai/model") is True
    for provider in ("together", "deepinfra", "azure", "openai", "", "not a slug"):
        assert provider_input_excludes_cache(provider, "anthropic/claude-x") is False
        assert provider_input_excludes_cache(provider) is False


def _settle_body(input_tokens: int, cache_read: int, cache_creation: int = 0) -> SimpleNamespace:
    return SimpleNamespace(input_count=input_tokens, cache_read_count=cache_read, cache_creation_count=cache_creation)


def test_normalized_prompt_accounting_reads_the_model_id() -> None:
    """input 4 beside 8 cached tokens: the exclusive convention bills 4
    uncached and records a 12-token prompt; the inclusive one (and the
    slug-only call, which keeps the old answer for callers that know the
    slug alone) clamps to 0 uncached and records 4."""
    body = _settle_body(4, 8)
    for provider in ("amazon-bedrock", "google-vertex"):
        assert normalized_prompt_accounting(provider, body, model_id="anthropic/claude-x") == (4, 12, 8, 0)
        assert normalized_prompt_accounting(provider, body, model_id="openai/gpt-oss-x") == (0, 4, 8, 0)
        assert normalized_prompt_accounting(provider, body) == (0, 4, 8, 0)
    assert normalized_prompt_accounting("anthropic", body, model_id="anthropic/claude-x") == (4, 12, 8, 0)
    assert normalized_prompt_accounting("anthropic", body) == (4, 12, 8, 0)
    assert normalized_prompt_accounting("together", body, model_id="anthropic/claude-x") == (0, 4, 8, 0)
    # A cache write counts the same way.
    assert normalized_prompt_accounting("amazon-bedrock", _settle_body(4, 8, 2), model_id="anthropic/claude-x") == (
        4, 14, 8, 2)
    # Without cached tokens the conventions agree.
    assert normalized_prompt_accounting("amazon-bedrock", _settle_body(4, 0), model_id="anthropic/claude-x") == (
        4, 4, 0, 0)


def _route(model_id: str, provider: str, tier: PriceTier):
    return ModelEndpoint(
        id=f"{model_id}@{provider}/prepaid", model_id=model_id, provider=provider, usage_type="Credits",
        prompt_price_microdollars_per_million_tokens=tier.prompt_price_microdollars_per_million_tokens,
        completion_price_microdollars_per_million_tokens=tier.completion_price_microdollars_per_million_tokens,
        price_tiers=(tier,), published_price_tiers=(tier,),
    )


def test_the_frozen_snapshot_reads_the_same_rule() -> None:
    """A candidate's prompt convention is the rule's over its provider and
    model id, so the frozen evaluation and the real path normalize the same
    usage the same way."""
    tier = PriceTier(None, 1_000_000, 2_000_000, 250_000)
    for provider, model_id, convention in (
        ("amazon-bedrock", "anthropic/claude-x", "excludes_cache"),
        ("google-vertex", "anthropic/claude-x", "excludes_cache"),
        ("amazon-bedrock", "openai/gpt-oss-x", "includes_cache"),
        ("anthropic", "anthropic/claude-x", "excludes_cache"),
        ("together", "anthropic/claude-x", "includes_cache"),
    ):
        route = _route(model_id, provider, tier)
        snapshot = billing.build_snapshot([route], billing.Eligibility())
        assert snapshot.candidates[0].prompt_convention == convention, (provider, model_id)
        raw = billing.RawUsage(input_tokens=4, output_tokens=0, cache_read_tokens=8, cache_creation_tokens=0)
        uncached, total, _read, _creation = normalized_prompt_accounting(
            provider, _settle_body(4, 8), model_id=model_id)
        if convention == "excludes_cache":
            result = billing.evaluate(snapshot, route.id, raw, billing.Eligibility())
            assert (result.usage.uncached_input_tokens, result.usage.total_prompt_tokens) == (4, 12) == (uncached, total)
            assert result.charge_micro == 4 + 2  # 4 at 1.00, 8 at 0.25
        else:
            # An inclusive count of 4 with 8 cached is malformed for the frozen
            # evaluation, which refuses rather than clamps (the enclave then
            # falls back to the synchronous settle, which clamps to 0 and 4).
            with pytest.raises(ValueError, match="malformed_usage"):
                billing.evaluate(snapshot, route.id, raw, billing.Eligibility())
            assert (uncached, total) == (0, 4)


def test_an_inclusive_count_below_its_cached_subset_is_refused_by_the_snapshot() -> None:
    tier = PriceTier(None, 1_000_000, 2_000_000, 250_000)
    route = _route("openai/gpt-oss-x", "amazon-bedrock", tier)
    snapshot = billing.build_snapshot([route], billing.Eligibility())
    with pytest.raises(ValueError, match="malformed_usage"):
        billing.evaluate(snapshot, route.id, billing.RawUsage(
            input_tokens=4, output_tokens=0, cache_read_tokens=8, cache_creation_tokens=0), billing.Eligibility())


# ------------------------------------------------------------ the real path --

TIER = PriceTier(
    max_prompt_tokens=None,
    prompt_price_microdollars_per_million_tokens=1_000_000,
    completion_price_microdollars_per_million_tokens=5_000_000,
    prompt_cached_price_microdollars_per_million_tokens=100_000,
)


def _client_and_key() -> tuple[TestClient, dict]:
    app = create_app(Settings(environment="test"), init_observability=False)
    client = TestClient(app)
    created = client.post(
        "/v1/keys",
        headers={"x-trustedrouter-user": "resold-claude@example.com"},
        json={"name": "resold claude cache accounting"},
    )
    assert created.status_code == 201, created.text
    return client, created.json()["data"]


def _serve(monkeypatch: pytest.MonkeyPatch, model_id: str, provider: str, upstream_id: str) -> None:
    drop_routes(monkeypatch, model_id)
    serve_on_fixture_route(
        monkeypatch, model_id, provider, author=model_id.partition("/")[0], upstream_id=upstream_id,
        completion_price_microdollars_per_million_tokens=5_000_000,
        price_tiers=(TIER,), published_price_tiers=(TIER,),
    )


def _settle(client: TestClient, key: dict, model_id: str, provider: str) -> tuple[dict, object]:
    authorize = client.post("/v1/internal/gateway/authorize", json={
        "api_key_hash": key["hash"], "model": model_id, "estimated_input_tokens": 8_000, "max_output_tokens": 100,
        "provider": {"only": [provider], "allow_fallbacks": False},
        # amazon-bedrock is admitted from a named, qualified gateway region.
        "region": "us-central1",
    })
    assert authorize.status_code == 200, authorize.text
    auth = authorize.json()["data"]
    endpoint = endpoint_for_id(auth["endpoint_id"])
    assert endpoint is not None and (endpoint.provider, endpoint.model_id) == (provider, model_id)
    settle = client.post("/v1/internal/gateway/settle", json={
        "authorization_id": auth["authorization_id"],
        # Anthropic-native usage: input 4 beside 8 cache reads.
        "actual_input_tokens": 4,
        "actual_output_tokens": 1,
        "cache_read_input_tokens": 8,
        "request_id": f"gw-resold-claude-{provider}",
        "elapsed_seconds": 1.0,
    })
    assert settle.status_code == 200, settle.text
    data = settle.json()["data"]
    generation = STORE.get_generation(data["generation_id"])
    assert generation is not None
    return data, generation


# 4 uncached input tokens at $1/M, 8 cache reads at the published $0.10/M
# and 1 output token at $5/M.
EXCLUSIVE_COST = 4 + 1 + 5
# The clamp: 0 uncached (4 - 8 < 0), 8 reads at $0.10/M, 1 output at $5/M.
INCLUSIVE_COST = 0 + 1 + 5


@pytest.mark.parametrize("model_id,provider,upstream_id", [
    (BEDROCK_CLAUDE, "amazon-bedrock", "us.anthropic.claude-sonnet-5"),
    (VERTEX_CLAUDE, "google-vertex", "claude-sonnet-4-6"),
])
def test_a_resold_claude_settle_bills_the_uncached_input_and_records_the_whole_prompt(
    model_id: str, provider: str, upstream_id: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before the rule read the model id, this settle billed 0 uncached input
    tokens (4 - 8 clamped) and recorded a 4-token prompt: 6 microdollars and
    4 tokens. Now: 4 uncached tokens billed and a 12-token prompt."""
    _serve(monkeypatch, model_id, provider, upstream_id)
    client, key = _client_and_key()
    data, generation = _settle(client, key, model_id, provider)
    assert data["cost_microdollars"] == EXCLUSIVE_COST == 10
    assert generation.tokens_prompt == 4 + 8 == 12
    assert generation.cached_input_tokens == 8
    # The slug-only reading, what the real path used to compute.
    assert normalized_prompt_accounting(provider, _settle_body(4, 8)) == (0, 4, 8, 0)
    assert INCLUSIVE_COST == 6


def test_a_non_claude_bedrock_settle_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """amazon-bedrock serving another publisher's model relays an inclusive
    count: 4 input tokens with 8 cached clamp to 0 uncached and a 4-token
    prompt, as before."""
    _serve(monkeypatch, BEDROCK_OTHER, "amazon-bedrock", "openai.gpt-oss-120b")
    client, key = _client_and_key()
    data, generation = _settle(client, key, BEDROCK_OTHER, "amazon-bedrock")
    assert data["cost_microdollars"] == INCLUSIVE_COST == 6
    assert generation.tokens_prompt == 4
    assert generation.cached_input_tokens == 8


def test_the_anthropic_slug_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, DIRECT_CLAUDE, "anthropic", "claude-haiku-4-5")
    client, key = _client_and_key()
    data, generation = _settle(client, key, DIRECT_CLAUDE, "anthropic")
    assert data["cost_microdollars"] == EXCLUSIVE_COST == 10
    assert generation.tokens_prompt == 12
    assert generation.cached_input_tokens == 8


# The supplement's resold-Claude vectors, by name. The frozen-main oracle
# (tests/test_async_settle_proof_oracle.py) leaves exactly these out, because
# its frozen reference cannot price them; the real path prices each of them
# here, through the synchronous settle route, to the vector's own literals.
RESOLD_CLAUDE_FIXTURE_CASES: tuple[str, ...] = (
    "bedrock_anthropic_cache",
    "bedrock_cache_rate_missing",
    "bedrock_other_publisher_cache",
    "mixed_providers_tiered_reseller",
    "vertex_anthropic_cache",
    "vertex_cache_rate_missing",
)


@pytest.mark.parametrize("name", RESOLD_CLAUDE_FIXTURE_CASES)
def test_a_resold_claude_fixture_case_settles_on_the_real_path(env: Any, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The synchronous settle route, with the vector's candidates as the
    catalog and its raw usage as the settle body, commits the vector's
    charge and records the vector's prompt total: the same numbers the
    frozen evaluation freezes for it. (env: the typed fake store, runtime
    and settings of tests/test_async_settle_handler.py.)"""
    (case,) = [case for case in CASES if case["name"] == name]
    assert case["expected_exclusion"] is None
    body, auth, _key = prepare(env, case)
    catalog(monkeypatch, body)
    client = _client(env[3])
    reply = client.post("/v1/internal/gateway/settle", json=legacy_body(body))
    assert reply.status_code == 200, reply.text
    data = reply.json()["data"]
    assert data["cost_microdollars"] == case["expected_charge_micro"]
    settled = env[0].get_gateway_authorization(auth.id)
    generation = env[0].get_generation(settled.finalized_generation_id)
    assert generation.total_cost_microdollars == case["expected_charge_micro"]
    assert generation.tokens_prompt == case["expected_normalized_usage"]["total_prompt_tokens"]
    assert generation.cached_input_tokens == case["expected_normalized_usage"]["cache_read_tokens"]


# ------------------------------------------- Stage D delivered-usage billing --

# The billing reviewer's scenario: a Stage D stream on Claude resold by
# amazon-bedrock, input 2,000 beside 100,000 cache reads and 500 output
# tokens, the enclave gone after a heartbeat, the reaper booking the
# snapshot. Prices as the catalog's for that route when the scenario ran
# ($2.321/M input, $0.2321/M cached, $11.606/M output), as a fixture.
STREAM_USAGE = {
    "input_tokens": 2_000, "output_tokens": 500, "cache_read_input_tokens": 100_000,
    "cache_creation_input_tokens": 0, "price_tier_input_tokens": 0, "reasoning_tokens": 0,
}
STREAM_TIER = PriceTier(None, 2_321_000, 11_606_000, 232_100)
# 2,000 uncached at 2.321 = 4,642; 100,000 cached at 0.2321 = 23,210; 500 output at 11.606 = 5,803.
EXCLUSIVE_STREAM_CHARGE = 4_642 + 23_210 + 5_803
# Read as cache-inclusive: 0 uncached (2,000 - 100,000 clamped), the same cached and output.
INCLUSIVE_STREAM_CHARGE = 0 + 23_210 + 5_803
STREAM_NOW = datetime(2026, 9, 2, tzinfo=UTC)

STREAMS = [
    ("anthropic/claude-sonnet-5", "amazon-bedrock", "us.anthropic.claude-sonnet-5", True),
    ("anthropic/claude-sonnet-4.6", "google-vertex", "claude-sonnet-4-6", True),
    ("openai/gpt-oss-120b", "amazon-bedrock", "openai.gpt-oss-120b", False),
]


def _stream_route(monkeypatch: pytest.MonkeyPatch, model_id: str, provider: str, upstream_id: str) -> ModelEndpoint:
    drop_routes(monkeypatch, model_id)
    return serve_on_fixture_route(
        monkeypatch, model_id, provider, author=model_id.partition("/")[0], upstream_id=upstream_id,
        prompt_price_microdollars_per_million_tokens=STREAM_TIER.prompt_price_microdollars_per_million_tokens,
        completion_price_microdollars_per_million_tokens=STREAM_TIER.completion_price_microdollars_per_million_tokens,
        price_tiers=(STREAM_TIER,), published_price_tiers=(STREAM_TIER,),
    )


def _stream_authorization(route: ModelEndpoint, *, prompt_tokens: int = 102_000) -> GatewayAuthorization:
    return GatewayAuthorization(
        id="gwa-resold-claude-stream", workspace_id="workspace", key_hash="key",
        model_id=route.model_id, provider=route.provider, usage_type=UsageType.CREDITS,
        estimated_microdollars=40_000, credit_reservation_id="reservation", settled=False,
        endpoint_id=route.id, candidate_endpoint_ids=[route.id],
        pricing_snapshot=canonical_pricing_snapshot(endpoint_pricing_document((route,))),
        heartbeat_seq=0, stage_d_reason="ok", stage_d_prompt_tokens=prompt_tokens, stage_d_max_output_tokens=500,
    )


def test_normalized_delivered_prompt_reads_the_model_id() -> None:
    exclusive, inclusive = (2_000, 102_000), (0, 2_000)
    for provider in ("amazon-bedrock", "google-vertex"):
        assert normalized_delivered_prompt(provider, STREAM_USAGE, model_id="anthropic/claude-x") == exclusive
        assert normalized_delivered_prompt(provider, STREAM_USAGE, model_id="openai/gpt-oss-x") == inclusive
        # The slug alone keeps the slug's answer.
        assert normalized_delivered_prompt(provider, STREAM_USAGE) == inclusive
    assert normalized_delivered_prompt("anthropic", STREAM_USAGE) == exclusive
    assert normalized_delivered_prompt("anthropic", STREAM_USAGE, model_id="anthropic/claude-x") == exclusive
    assert normalized_delivered_prompt("together", STREAM_USAGE, model_id="anthropic/claude-x") == inclusive


@pytest.mark.parametrize(("model_id", "provider", "upstream_id", "exclusive"), STREAMS)
def test_a_streams_delivered_usage_is_priced_in_the_selected_endpoints_convention(
    model_id: str, provider: str, upstream_id: str, exclusive: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The heartbeat's running charge and the reaper's booking read the
    delivered usage by the shared rule: Claude resold by amazon-bedrock or
    google-vertex bills its 2,000 uncached input tokens (33,655 microdollars
    at the fixture prices; the slug-only reading billed 29,013 and recorded a
    2,000-token prompt); another publisher's model on the same reseller is
    unchanged."""
    route = _stream_route(monkeypatch, model_id, provider, upstream_id)
    authorization = _stream_authorization(route)
    document = parse_pricing_snapshot(authorization.pricing_snapshot or "")
    charge = delivered_usage_charge_microdollars(authorization, document, provider, route.id, STREAM_USAGE)
    uncached, total = normalized_delivered_prompt(provider, STREAM_USAGE, model_id=route.model_id)
    if exclusive:
        assert charge == EXCLUSIVE_STREAM_CHARGE == 33_655
        assert (uncached, total) == (2_000, 102_000)
    else:
        assert charge == INCLUSIVE_STREAM_CHARGE == 29_013
        assert (uncached, total) == (0, 2_000)
    # What the slug-only reading computed for every one of them.
    assert delivered_usage_cost_microdollars(document, provider, route.id, STREAM_USAGE) == 29_013
    assert normalized_delivered_prompt(provider, STREAM_USAGE) == (0, 2_000)


def _seed_stream(db: FakeSpannerDatabase, authorization: GatewayAuthorization) -> None:
    def seed(transaction: Any) -> None:
        insert_reservation(
            transaction, _ParamTypes, reservation_id="reservation", workspace_id="workspace", key_hash="key",
            ws_shard=0, credit_shard=0, key_shard=0, credit_reserved_micro=40_000, key_reserved_micro=40_000,
            hold_usage_type="Credits", authorization_id=authorization.id, idempotency_scope=None,
            idempotency_fingerprint=None, expires_at=STREAM_NOW + timedelta(seconds=30),
        )
        insert_gateway_authorization(transaction, _ParamTypes, authorization, created_at=STREAM_NOW)

    db.run_in_transaction(seed)
    db.typed.setdefault(CREDIT_BALANCE_TABLE, {})[("workspace", 0)] = {
        "workspace_id": "workspace", "shard": 0, "total_credits": 1_000_000, "total_usage": 0, "reserved": 40_000,
        "source_updated_at": None, "updated_at": None,
    }
    db.typed.setdefault(KEY_LIMIT_TABLE, {})[("key", 0)] = {
        "key_hash": "key", "shard": 0, "limit_microdollars": 1_000_000, "include_byok": True, "usage": 0,
        "byok_usage": 0, "reserved": 40_000, "day_usage": 0, "day_start": None, "week_usage": 0, "week_start": None,
        "month_usage": 0, "month_start": None,
    }


def _stream_heartbeat(db: FakeSpannerDatabase, route: ModelEndpoint) -> HeartbeatResult:
    return heartbeat_gateway_atomic(
        db, _ParamTypes, authorization_id="gwa-resold-claude-stream", seq=1, started_at=STREAM_NOW,
        selected_endpoint_id=route.id, usage=dict(STREAM_USAGE), heartbeat_hash="a" * 64, stream=True,
        grace_seconds=300, now=STREAM_NOW,
    )


@pytest.mark.parametrize(("model_id", "provider", "upstream_id", "exclusive"), STREAMS)
def test_the_reaper_books_a_streams_snapshot_in_the_selected_endpoints_convention(
    model_id: str, provider: str, upstream_id: str, exclusive: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reviewer's scenario end to end: the heartbeat's running charge,
    then the reaper's snapshot booking (actual_micro, the finalized cost and
    the generation's prompt total) after the enclave is gone."""
    route = _stream_route(monkeypatch, model_id, provider, upstream_id)
    authorization = _stream_authorization(route)
    db = FakeSpannerDatabase(now=STREAM_NOW)
    _seed_stream(db, authorization)
    charge = EXCLUSIVE_STREAM_CHARGE if exclusive else INCLUSIVE_STREAM_CHARGE
    prompt_total = 102_000 if exclusive else 2_000

    heartbeat = _stream_heartbeat(db, route)
    assert heartbeat.accepted, heartbeat
    assert heartbeat.running_micro == charge

    result = reap_expired_reservations_result(
        db, _ParamTypes, now=STREAM_NOW + timedelta(seconds=301), snapshot_booking_enabled=True)
    assert result.count == 1 and result.snapshot_bookings == 1 and result.outcome_counts["snapshot_booked"] == 1
    assert db.reservations["reservation"]["actual_micro"] == charge
    assert db.typed[CREDIT_BALANCE_TABLE][("workspace", 0)]["total_usage"] == charge
    stored = db.gateway_authorizations["gwa-resold-claude-stream"]
    assert stored["finalization_outcome"] == "reaped_snapshot"
    assert stored["finalized_cost_microdollars"] == charge
    (record,) = db.generation_records.values()
    generation = json.loads(record["payload"])
    assert generation["settled_from"] == "heartbeat"
    assert generation["tokens_prompt"] == prompt_total
    assert generation["cached_input_tokens"] == 100_000
    assert generation["total_cost_microdollars"] == charge
    # snapshot_generation, the reap's generation as the drain rebuilds it, agrees.
    with db.snapshot() as reader:
        reread = read_gateway_authorization(reader, _ParamTypes, authorization.id)
    assert reread is not None
    rebuilt = snapshot_generation(reread, actual_micro=charge)
    assert rebuilt is not None and rebuilt.tokens_prompt == prompt_total


@pytest.mark.parametrize(("model_id", "provider", "upstream_id", "exclusive"), STREAMS)
def test_the_heartbeat_cap_reads_the_whole_prompt_of_a_resold_claude_stream(
    model_id: str, provider: str, upstream_id: str, exclusive: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The authorized-token cap compares the delivered prompt total in the
    selected endpoint's convention: 102,000 tokens for resold Claude (as for
    the anthropic slug), 2,000 for another publisher's model on the same
    reseller. A limit of 2,000 prompt tokens therefore refuses the resold
    Claude stream's heartbeat and admits the other."""
    route = _stream_route(monkeypatch, model_id, provider, upstream_id)
    authorization = _stream_authorization(route, prompt_tokens=2_000)
    db = FakeSpannerDatabase(now=STREAM_NOW)
    _seed_stream(db, authorization)
    heartbeat = _stream_heartbeat(db, route)
    if exclusive:
        assert not heartbeat.accepted and heartbeat.reason == "usage_exceeds_cap"
    else:
        assert heartbeat.accepted and heartbeat.running_micro == INCLUSIVE_STREAM_CHARGE


def test_every_delivered_prompt_caller_passes_the_model_id() -> None:
    """Every call of normalized_delivered_prompt and of
    delivered_usage_cost_microdollars in the router passes model_id=, so no
    delivered-usage site reads the slug alone."""
    import ast

    from trusted_router import storage_gcp_authorize, storage_gcp_stage_d

    calls: dict[str, int] = {}
    for module in (storage_gcp_stage_d, storage_gcp_authorize):
        assert module.__file__ is not None
        tree = ast.parse(Path(module.__file__).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
            if name in ("normalized_delivered_prompt", "delivered_usage_cost_microdollars"):
                assert "model_id" in {keyword.arg for keyword in node.keywords}, (module.__name__, ast.unparse(node))
                calls[name] = calls.get(name, 0) + 1
    assert calls == {"normalized_delivered_prompt": 4, "delivered_usage_cost_microdollars": 1}


def test_the_settle_path_passes_the_selected_endpoints_model_id() -> None:
    """The real path's one call site reads the selected endpoint's provider
    and model id, the same two keys the frozen snapshot's candidate has."""
    import ast
    import inspect

    source = inspect.getsource(gateway._settle_gateway_authorization)
    calls = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "normalized_prompt_accounting"]
    assert len(calls) == 1
    (call,) = calls
    assert ast.unparse(call) == "normalized_prompt_accounting(selected_endpoint.provider, body, model_id=selected_endpoint.model_id)"
