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

from trusted_router.catalog_data import PROVIDERS, Model, ModelEndpoint  # noqa: E402
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
    RoutingSnapshotV1,
    SnapshotSettings,
    StageDSettings,
    VariantSuffix,
    canonical_snapshot_bytes,
    compile_routing_snapshot,
)
from trusted_router.routing_snapshot_signing import (  # noqa: E402
    PURPOSE,
    RoutingSnapshotSigner,
)

OUTPUT_BODY = ROOT / "fastpath" / "testdata" / "routing_snapshot_v1.body.json"
OUTPUT_MANIFEST = ROOT / "fastpath" / "testdata" / "routing_snapshot_v1.manifest.json"
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
    kid="routing-snapshot-test-key-1",
    purpose=PURPOSE,
    public_key_b64url=b64encode(
        Ed25519PrivateKey.from_private_bytes(TEST_KEY_SEED).public_key().public_bytes_raw()
    ),
    iss="tr-control-plane-test",
    aud="tr-fastpath-edge-test",
)

_PROVIDER_SLUGS = (
    "amazon-bedrock", "deepinfra", "deepseek", "google-ai-studio", "minimax",
    "sakana", "tencent", "thinkingmachines", "trustedrouter", "wafer", "zai",
)


def _model(model_id: str, provider: str) -> Model:
    return Model(id=model_id, name=model_id, provider=provider, context_length=131_072)


def _endpoint(model_id: str, provider: str, usage: str, prompt: int, completion: int,
              **extra: object) -> ModelEndpoint:
    suffix = "byok" if usage == "BYOK" else "prepaid"
    fields: dict[str, object] = {
        "upstream_id": None,
        "supported_parameters": ("tools", "max_tokens", "response_format"),
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
        _endpoint(glm, "wafer", "Credits", 1_000_000, 3_200_000, upstream_id="GLM-5.3"),
        # Bedrock: only the upgraded gateway regions, and only an explicit origin.
        _endpoint(bedrock, "amazon-bedrock", "Credits", 316_500, 1_266_000),
        # Sakana's terms exclude the EEA: no europe-west4 gateway.
        _endpoint(fugu, "sakana", "Credits", 5_275_000, 21_100_000),
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
    )
    models = (
        _model(flash, "deepseek"), _model(tencent_flash, "deepseek"), _model(glm, "zai"),
        _model(bedrock, "minimax"), _model(fugu, "sakana"), _model(gemini, "google-ai-studio"),
        _model(inkling, "thinkingmachines"),
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
        expansions_clock=(COMPILED_AT, COMPILED_AT),
        aliases=aliases,
        decision_chains={"trustedrouter/dev-1.0": ("wafer", "deepinfra")},
        ranking_evidence=evidence,
        prepaid_min_rank={"tencent": 1_500},
        model_provider_preference={tencent_flash: {"tencent": 3_000}, glm: {"wafer": -1}},
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


def snapshot() -> RoutingSnapshotV1:
    return compile_routing_snapshot(
        at=COMPILED_AT, horizon=HORIZON_SECONDS, settings=fixture_settings(),
        source_revision="golden-fixture", plane=Plane(cloud="gcp", region="us-central1"),
        inputs=fixture_inputs(),
    )


def golden_signer() -> RoutingSnapshotSigner:
    return RoutingSnapshotSigner(Ed25519PrivateKey.from_private_bytes(TEST_KEY_SEED), TEST_KEY)


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
