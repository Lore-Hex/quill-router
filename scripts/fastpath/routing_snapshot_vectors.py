"""Golden files for the Go routing-snapshot reader (E2, docs/design/routing-snapshot-v1.md).

Compiles a small synthetic catalog with `trusted_router.routing_snapshot` at
2026-10-19T00:00Z over a 6 h horizon and signs it with a TEST key derived from
a fixed seed below. It writes the exact canonical body (no trailing newline)
and the signed manifest to fastpath/testdata/. The endpoints sit on real
provider slugs so the real price schedules, privacy facts, region holds and
retirements apply; prices, models, rankings and settings are synthetic.
tests/test_fastpath_routing_snapshot_vectors.py fails when the files are not
what this script writes now.

    uv run python scripts/fastpath/routing_snapshot_vectors.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)

from trusted_router.catalog_data import (  # noqa: E402
    PRIVACY_TIER_ALIASES,
    PROVIDERS,
    Model,
    ModelEndpoint,
)
from trusted_router.detached_jws import TrustedKey, b64encode  # noqa: E402
from trusted_router.pricing import PriceTier  # noqa: E402
from trusted_router.routing_snapshot import (  # noqa: E402
    NO_OVERRIDES,
    Aliases,
    CatalogInputs,
    Plane,
    PrefixRewrite,
    RegionEntry,
    RequestedModel,
    RequestedOverrides,
    RoutingSnapshotV3,
    SnapshotSettings,
    StageDSettings,
    VariantSuffix,
    canonical_snapshot_bytes,
    compile_routing_snapshot,
    live_parameter_rules,
)
from trusted_router.routing_snapshot_signing import (  # noqa: E402
    PURPOSE,
    TEST_KID,
    RoutingSnapshotSigner,
)

OUTPUT_BODY = ROOT / "fastpath" / "testdata" / "routing_snapshot_v3.body.json"
OUTPUT_MANIFEST = ROOT / "fastpath" / "testdata" / "routing_snapshot_v3.manifest.json"
GENERATOR = "scripts/fastpath/routing_snapshot_vectors.py"

COMPILED_AT = int(datetime(2026, 10, 19, tzinfo=UTC).timestamp())
HORIZON_SECONDS = 6 * 3_600
# A TEST key, public by construction: the seed is this repository's text.
# Ed25519 derives the same key from the same seed, so the golden signature is
# reproducible. No production key is, or may be, derived this way.
TEST_KEY_SEED = hashlib.sha256(
    b"trustedrouter routing snapshot v1 golden-file TEST key; not a secret"
).digest()
TEST_KEY = TrustedKey(
    kid=TEST_KID,
    purpose=PURPOSE,
    public_key_b64url=b64encode(
        Ed25519PrivateKey.from_private_bytes(TEST_KEY_SEED).public_key().public_bytes_raw()
    ),
    iss="tr-control-plane-test",
    aud="tr-fastpath-edge-test",
)

_PROVIDER_SLUGS = (
    "amazon-bedrock", "chutes", "deepinfra", "deepseek", "fireworks", "google-ai-studio", "kimi",
    "minimax", "sakana", "tencent", "thinkingmachines", "tinfoil", "together", "trustedrouter",
    "wafer", "zai",
)
# Every route declares these unless it says otherwise; the routing cases
# exercise routes that lack tools, or structured output, or declare the
# structured_outputs spelling.
DEFAULT_PARAMETERS = ("tools", "max_tokens", "response_format")


def _model(model_id: str, provider: str, context_length: int = 131_072) -> Model:
    return Model(id=model_id, name=model_id, provider=provider, context_length=context_length)


def _endpoint(model_id: str, provider: str, usage: str, prompt: int, completion: int,
              **extra: object) -> ModelEndpoint:
    suffix = "byok" if usage == "BYOK" else "prepaid"
    fields: dict[str, object] = {
        "upstream_id": None,
        "supported_parameters": DEFAULT_PARAMETERS,
        "input_modalities": ("text",),
        "price_tiers": (PriceTier(None, prompt, completion, prompt // 10),),
        **extra,
    }
    return ModelEndpoint(
        id=f"{model_id}@{provider}/{suffix}", model_id=model_id, provider=provider,
        usage_type=usage,
        prompt_price_microdollars_per_million_tokens=prompt,
        completion_price_microdollars_per_million_tokens=completion,
        published_prompt_price_microdollars_per_million_tokens=prompt,
        published_completion_price_microdollars_per_million_tokens=completion,
        **fields,
    )


def fixture_inputs() -> CatalogInputs:
    flash, glm, gemini = "deepseek/deepseek-v4-flash", "z-ai/glm-5.3", "google/gemini-3.1-pro-preview"
    tencent_flash, fugu = "deepseek/deepseek-v4.1-flash", "sakana-ai/fugu-ultra-v1.1"
    bedrock, inkling = "minimax/minimax-m2", "thinkingmachines/inkling"
    kimi = "moonshotai/kimi-k2.7"
    two_tiers = (
        PriceTier(200_000, 2_110_000, 12_660_000, 211_000),
        PriceTier(None, 4_220_000, 18_990_000, 422_000),
    )
    endpoints = (
        # DeepSeek direct: the V4.1 Flash weekday peak, 01:00-04:00 UTC.
        _endpoint(flash, "deepseek", "Credits", 160_000, 640_000, upstream_id="deepseek-v4-flash"),
        # Tencent Singapore: its own peak, Beijing 09:00-12:00 = 01:00-04:00 UTC.
        _endpoint(tencent_flash, "tencent", "Credits", 158_250, 633_000,
                  input_modalities=("text", "image")),
        # Wafer's catalog reports ZDR for GLM 5.3: wafer_zdr_required.
        _endpoint(glm, "wafer", "Credits", 1_000_000, 3_200_000, upstream_id="GLM-5.3",
                  context_length=100_000),
        # More hosts for GLM 5.3 and V4.1 Flash, so the routing cases order
        # several candidates: by rank evidence, preference, price and filters.
        # Together's routes declare no tools where another route of the model
        # does, so a tools request drops them; Fireworks' declares no
        # structured output, which only require_parameters holds it to.
        _endpoint(glm, "together", "Credits", 950_000, 3_300_000,
                  supported_parameters=("max_tokens", "response_format")),
        _endpoint(glm, "fireworks", "Credits", 1_100_000, 3_000_000,
                  supported_parameters=("max_tokens", "tools")),
        _endpoint(tencent_flash, "together", "Credits", 160_000, 640_000,
                  supported_parameters=("max_tokens", "response_format")),
        # Bedrock: only the upgraded gateway regions, and only an explicit origin.
        _endpoint(bedrock, "amazon-bedrock", "Credits", 316_500, 1_266_000),
        # Sakana's terms exclude the EEA: no europe-west4 gateway. No route of
        # Fugu declares tools: silence is a gap, not a refusal.
        _endpoint(fugu, "sakana", "Credits", 5_275_000, 21_100_000,
                  supported_parameters=("max_tokens",)),
        # Two price tiers, and its BYOK twin (two BYOK storage slugs).
        _endpoint(gemini, "google-ai-studio", "Credits", 2_110_000, 12_660_000,
                  price_tiers=two_tiers),
        _endpoint(gemini, "google-ai-studio", "BYOK", 2_110_000, 12_660_000,
                  price_tiers=two_tiers),
        # A catalog deadline inside the horizon (it shortens not_after), and a
        # real scheduled retirement after it.
        _endpoint(inkling, "deepinfra", "Credits", 1_055_000, 4_220_000,
                  upstream_id="thinkingmachines/Inkling",
                  catalog_valid_until=datetime(2026, 10, 19, 5, 30, tzinfo=UTC)),
        # Confidential hosts for GLM 5.3, priced under the others so no
        # earlier estimate moves: Tinfoil meets every privacy requirement
        # (Trusted), Chutes Confidential but not Trusted. Tinfoil declares the
        # structured_outputs spelling, which require_parameters accepts for
        # response_format, and logprobs, which no other route declares.
        _endpoint(glm, "tinfoil", "Credits", 800_000, 2_800_000,
                  supported_parameters=("tools", "max_tokens", "structured_outputs", "logprobs")),
        _endpoint(glm, "chutes", "Credits", 850_000, 2_700_000),
        # Kimi K2.7 on two Confidential hosts only. Its preference for Chutes
        # (below) applies only when it is the request's one model, so a
        # privacy floor that removes every route of the model before it
        # reorders Kimi's hosts.
        _endpoint(kimi, "tinfoil", "Credits", 600_000, 2_500_000),
        _endpoint(kimi, "chutes", "Credits", 550_000, 2_200_000),
    )
    # Context windows: three sizes for the routing cases' context filter, and
    # Gemini's unknown (0), which the filter keeps whatever the request's size.
    models = (
        _model(flash, "deepseek", 65_536), _model(tencent_flash, "deepseek", 32_768),
        _model(glm, "zai"), _model(bedrock, "minimax"), _model(fugu, "sakana"),
        _model(gemini, "google-ai-studio", 0), _model(inkling, "thinkingmachines"),
        _model(kimi, "kimi"),
    )
    credits = RequestedOverrides(**{**NO_OVERRIDES.model_dump(), "usage": "Credits"})
    requested = (
        RequestedModel(id="trustedrouter/auto", expands_to=(flash, glm, gemini),
                       refused_status=None, private_proxy=False, overrides=credits),
        RequestedModel(
            id="trustedrouter/zdr", expands_to=(glm, gemini), refused_status=None,
            private_proxy=False,
            overrides=credits.model_copy(update={
                "privacy_requirements": (2,), "order": ("google-ai-studio", "wafer"),
            }),
        ),
        RequestedModel(id="trustedrouter/dev-1.0", expands_to=(flash,), refused_status=None,
                       private_proxy=True, overrides=credits),
        RequestedModel(id="trustedrouter/synth", expands_to=(), refused_status=501,
                       private_proxy=False, overrides=NO_OVERRIDES),
    )
    aliases = Aliases(
        router_prefix_rewrites=(PrefixRewrite(prefix="nyte/", replacement="trustedrouter/"),),
        router_base_rewrites={"trustedrouter/auto-routing": "trustedrouter/auto"},
        alias_targets={"trustedrouter/confidential": "trustedrouter/e2e"},
        dated_suffix_pattern=r"-\d{4}-\d{2}-\d{2}$",
        bare_vendor_prefix="openai/",
        variant_suffixes=(
            VariantSuffix(suffix=":nitro", preference="sort", value="throughput"),
            VariantSuffix(suffix=":floor", preference="sort", value="price"),
        ),
        provider_aliases={"ai-studio": "google-ai-studio", "z-ai": "zai"},
        provider_group_aliases={"google": ("google-vertex", "google-ai-studio")},
        router_provider_slugs=("openrouter", "trustedrouter"),
        privacy_tiers=dict(PRIVACY_TIER_ALIASES),
    )
    row = {"completion_rate": 1.0, "p50_ttft_ms": 900, "tokens_per_second": 120.5,
           "samples": 40, "throughput_samples": 9}
    evidence = {
        "window": "7d",
        "generated_at": "2026-10-18T12:00:00.250Z",
        "providers": [
            {**row, "provider": "deepseek"},
            {**row, "provider": "wafer", "p50_ttft_ms": 450, "tokens_per_second": 300.25},
            {**row, "provider": "deepinfra", "completion_rate": 0.9},
            {**row, "provider": "tencent", "samples": 3},
        ],
    }
    return CatalogInputs(
        providers={slug: PROVIDERS[slug] for slug in _PROVIDER_SLUGS},
        models=models,
        endpoints=endpoints,
        requested_models=requested,
        catalog_resolved_at=COMPILED_AT,
        expansions_clock=(COMPILED_AT, COMPILED_AT),
        aliases=aliases,
        parameter_rules=live_parameter_rules(),
        decision_chains={"trustedrouter/dev-1.0": ("wafer", "deepinfra")},
        ranking_evidence=evidence,
        prepaid_min_rank={"tencent": 1_500},
        model_provider_preference={tencent_flash: {"tencent": 3_000}, glm: {"wafer": -1},
                                   kimi: {"chutes": -1}},
    )


def fixture_settings() -> SnapshotSettings:
    regions = ("us-central1", "us-east4", "europe-west4", "us-west1")
    return SnapshotSettings(
        regions=regions,
        primary_region="us-central1",
        region_payload=tuple(
            RegionEntry(
                id=region, name=region, primary=region == "us-central1", enabled=True,
                api_base_url=(
                    "https://api.trustedrouter.com/v1" if region == "us-central1"
                    else f"https://api-{region}.quillrouter.com/v1"
                ),
                control_plane_url=None,
            )
            for region in regions
        ),
        stage_d=StageDSettings(
            eligibility_enabled=True, heartbeat_enabled=True, require_heartbeat_declaration=False,
            # Synthetic test values, never a real workspace.
            pilot_workspaces=("ws_test_pilot_a", "ws_test_pilot_b"),
        ),
        request_record_write_mode="typed",
    )


def snapshot() -> RoutingSnapshotV3:
    return compile_routing_snapshot(
        at=COMPILED_AT, horizon=HORIZON_SECONDS, settings=fixture_settings(),
        source_revision="golden-fixture", plane=Plane(cloud="gcp", region="us-central1"),
        inputs=fixture_inputs(),
    )


def golden_signer() -> RoutingSnapshotSigner:
    return RoutingSnapshotSigner(
        Ed25519PrivateKey.from_private_bytes(TEST_KEY_SEED), TEST_KEY, allow_test_key=True,
    )


def render_body() -> bytes:
    return canonical_snapshot_bytes(snapshot())


def render_manifest() -> str:
    manifest = {
        "generator": GENERATOR,
        "test_key": True,
        "kid": TEST_KEY.kid,
        "purpose": TEST_KEY.purpose,
        "iss": TEST_KEY.iss,
        "aud": TEST_KEY.aud,
        "public_key_b64url": TEST_KEY.public_key_b64url,
        "jws": golden_signer().sign(render_body()),
    }
    return json.dumps(manifest, indent=1, sort_keys=True) + "\n"


if __name__ == "__main__":
    OUTPUT_BODY.write_bytes(render_body())
    OUTPUT_MANIFEST.write_text(render_manifest())
    print(f"wrote {OUTPUT_BODY.relative_to(ROOT)} and {OUTPUT_MANIFEST.relative_to(ROOT)}")
