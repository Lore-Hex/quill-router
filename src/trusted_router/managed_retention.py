"""Reviewed managed-account retention, independent of automatic model discovery.

A provider's ZDR contract does not approve future models or safety-retention
products. OpenAI/Vertex reviews restrict existing account claims. Bedrock
Runtime additionally enforces its reviewed regional policy before each call;
its exact-profile approval never upgrades the provider-wide Mantle posture.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from trusted_router.provider_contracts import BEDROCK_RUNTIME_PROFILES

RetentionMode = Literal[
    "zero_retention", "private_safety_processing", "safety_retention",
    "durable_cache", "unverified",
]

OPENAI_DATA_URL = "https://developers.openai.com/api/docs/guides/your-data"
OPENAI_PSP_URL = "https://developers.openai.com/api/docs/guides/private-safety-processing"
VERTEX_DATA_URL = (
    "https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/zero-data-retention"
)
VERTEX_SAFETY_URL = (
    "https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/abuse-monitoring"
)
BEDROCK_RETENTION_URL = "https://docs.aws.amazon.com/bedrock/latest/userguide/data-retention.html"


@dataclass(frozen=True)
class RetentionPolicy:
    mode: RetentionMode
    label: str
    detail: str
    source_url: str
    # Bounds are separate: OpenAI PSP mandates AT LEAST 30 days, not a
    # universal maximum of 30 days for every customer-controlled bucket.
    minimum_days: int | None = None
    maximum_days: int | None = None
    storage_control: str | None = None
    human_review: bool | None = None
    reviewed_on: str = "2026-10-28"

    @property
    def permits_strict_zdr(self) -> bool:
        return self.mode == "zero_retention"

    def metadata(self) -> dict[str, object]:
        return {**asdict(self), "permits_strict_zdr": self.permits_strict_zdr}


OPENAI_ZDR = RetentionPolicy(
    "zero_retention", "ZDR - TR-funded",
    "The managed OpenAI organization and Default project use ordinary Zero Data "
    "Retention, not PSP, as verified on October 28, 2026. This exact existing "
    "model is reviewed; future models do not inherit approval. BYOK is not covered.",
    OPENAI_DATA_URL,
)
OPENAI_PSP = RetentionPolicy(
    "private_safety_processing", "Private safety processing (PSP)",
    "Encrypted safety records are retained in customer-controlled storage for at "
    "least 30 days and reviewed automatically in an attested runtime. For a managed "
    "route, customer-controlled means TrustedRouter-controlled, not the end user's "
    "storage. This is not strict ZDR or proof of confidential model inference.",
    OPENAI_PSP_URL, minimum_days=30, storage_control="trustedrouter", human_review=False,
)
OPENAI_DURABLE_CACHE = RetentionPolicy(
    "durable_cache", "Encrypted cache - up to 24 hours",
    "This model may retain encrypted key/value tensors in GPU-local storage for "
    "up to 24 hours, even on an ordinary ZDR account. GPT-5.5 rejects in-memory-only "
    "retention; newer families' cache TTL is a minimum, not a deletion deadline. "
    "No verified in-memory-only configuration is enforced for this route, so it "
    "is excluded from strict ZDR. This is not PSP safety logging.",
    "https://developers.openai.com/api/docs/guides/prompt-caching",
    maximum_days=1, storage_control="openai",
)
VERTEX_ZDR = RetentionPolicy(
    "zero_retention", "ZDR - TR-funded",
    "This reviewed route uses TrustedRouter's contracted managed Vertex account. "
    "Safety-retention models, durable caching and unreviewed routes are excluded "
    "from strict ZDR. No Google Search/Maps grounding or Live session resumption.",
    VERTEX_DATA_URL,
)
VERTEX_DURABLE_CACHE = RetentionPolicy(
    "durable_cache", "Durable cache - up to 24 hours",
    "The managed Vertex project was verified with DURABLE implicit caching enabled "
    "on October 28, 2026. Eligible Gemini inputs may persist in encrypted storage "
    "for up to 24 hours. This route is excluded from strict ZDR pending remediation "
    "and a fresh audit; no-training does not mean no retention.",
    VERTEX_DATA_URL, maximum_days=1, storage_control="google",
)
VERTEX_SAFETY_RETENTION = RetentionPolicy(
    "safety_retention", "Safety retention - up to 30 days",
    "Vertex Advanced AI logs prompts and responses for up to 30 days for abuse "
    "monitoring, with authorized human review possible. These logs are not "
    "CMEK-encrypted. This is not strict ZDR and is not OpenAI's PSP product.",
    VERTEX_SAFETY_URL, maximum_days=30, storage_control="google", human_review=True,
)
VERTEX_FRONTIER_RETENTION = RetentionPolicy(
    "safety_retention", "Frontier safety retention - up to 30 days",
    "Google Frontier Model safety logs can be retained in the customer's environment "
    "for up to 30 days. Human review depends on the governing agreement; no "
    "human-review exemption is inferred here. Not strict ZDR, regardless of Google's "
    "ZDR-aligned terminology. This is separate from OpenAI PSP.",
    VERTEX_SAFETY_URL, maximum_days=30, storage_control="customer_environment",
)
BEDROCK_RUNTIME_ZDR = RetentionPolicy(
    "zero_retention", "ZDR - managed Bedrock Runtime",
    "This exact US Claude inference profile uses the managed Bedrock Runtime "
    "account with retention set to none in us-east-1, us-east-2 and us-west-2. "
    "The gateway verifies all three policies before every inference and refuses "
    "unverified policies. This does not cover Mantle, BYOK, global profiles or "
    "other models. It is policy-backed ZDR, not confidential compute or E2EE.",
    BEDROCK_RETENTION_URL, reviewed_on="2026-10-29",
)

# Frozen review, not generated from provider manifests. Discovery must never
# append to this allowlist automatically. A mutable cross-model alias such as
# chat-latest intentionally stays unverified.
_OPENAI_REVIEWED_IDS = frozenset({
    "gpt-3.5-turbo", "gpt-3.5-turbo-0125", "gpt-4-0613",
    "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "gpt-4o", "gpt-4o-mini",
    "gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-5-pro", "gpt-5.1",
    "gpt-5.2", "gpt-5.2-pro", "gpt-5.3-codex",
    "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.4-pro",
    "gpt-image-2.5-flare", "gpt-image-2.5-sunburst",
    "o1", "o1-pro", "o3", "o3-mini", "o3-pro", "o4-mini",
    "text-embedding-3-small", "text-embedding-3-large", "text-embedding-ada-002",
})

# Exact canonical/upstream pairs prevent a catalog alias from changing the
# deployed model while retaining its predecessor's privacy approval.
REVIEWED_ROUTES: dict[tuple[str, str, str], RetentionPolicy] = {
    **{("amazon-bedrock", model, profile): BEDROCK_RUNTIME_ZDR
       for model, profile in BEDROCK_RUNTIME_PROFILES.items()},
    **{("openai", f"openai/{name}", name): OPENAI_ZDR for name in _OPENAI_REVIEWED_IDS},
    **{
        ("openai", f"openai/{name}", name): OPENAI_DURABLE_CACHE
        for name in (
            "gpt-5.5", "gpt-5.5-pro", "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra",
            "gpt-6-astra", "gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol",
        )
    },
    **{
        ("google-vertex", f"google/{name}", name): VERTEX_ZDR
        for name in ("gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro")
    },
    ("google-vertex", "anthropic/claude-sonnet-4.6", "claude-sonnet-4-6"): VERTEX_ZDR,
}

_VERTEX_DURABLE_MODELS = frozenset({
    "gemini-3.1-flash-lite", "gemini-3.1-pro-preview", "gemini-3.5-flash",
    "gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-3.7-flash",
    "gemini-3.8-flash", "gemini-4-argon",
})


def managed_retention_policy(
    provider: str, model_id: str, upstream_id: str | None, usage_type: str,
) -> RetentionPolicy | None:
    if provider not in {"openai", "google-vertex", "amazon-bedrock"}:
        return None
    source = {"openai": OPENAI_DATA_URL, "google-vertex": VERTEX_DATA_URL,
              "amazon-bedrock": BEDROCK_RETENTION_URL}[provider]
    if usage_type != "Credits":
        return RetentionPolicy(
            "unverified", "Customer account retention - unverified",
            "TrustedRouter's managed-account data controls do not apply to BYOK. "
            "This credential path is excluded from strict ZDR.", source,
        )
    upstream = upstream_id or ""
    if provider == "google-vertex":
        if any("claude-fable" in value or "claude-mythos" in value for value in (model_id, upstream)):
            return VERTEX_SAFETY_RETENTION
        if any("gemini-4-argon" in value and "cyber" in value.casefold() for value in (model_id, upstream)):
            return VERTEX_FRONTIER_RETENTION
        if upstream in _VERTEX_DURABLE_MODELS:
            return VERTEX_DURABLE_CACHE
    reviewed = REVIEWED_ROUTES.get((provider, model_id, upstream))
    if reviewed is not None:
        return reviewed
    return RetentionPolicy(
        "unverified", "Retention unverified - not strict ZDR",
        "This exact model and upstream ID have not been approved for strict ZDR "
        "under the current account controls. Provider-wide ZDR, no-training and "
        "successful inference are insufficient. Safety retention or other content "
        "storage may apply; the route is excluded until reviewed.", source,
    )


def provider_retention_notice(provider: str) -> str | None:
    if provider == "amazon-bedrock":
        return (
            "ZDR is scoped to the reviewed managed Claude US Runtime profiles, "
            "with retention none verified in all three US destination regions "
            "before every request. Mantle and unreviewed profiles are not covered. "
            "Only routes whose gateway rollout has completed are available."
        )
    if provider == "openai":
        return (
            "Ordinary ZDR is active on the reviewed managed project (checked October 28, "
            "2026). PSP is not enabled there. Future models need their own review. "
            "Models with persistent cache state are excluded from strict ZDR even "
            "on this ordinary ZDR account. "
            "If PSP is enabled, encrypted safety records remain in TrustedRouter-controlled "
            "storage for at least 30 days; those routes must be labeled PSP and excluded "
            "from strict ZDR. BYOK never inherits our project settings."
        )
    if provider == "google-vertex":
        return (
            "Retention varies by model and feature. Durable caching is enabled on the "
            "managed project (checked October 28, 2026); affected Gemini routes are not "
            "strict ZDR. Advanced AI and Google Frontier safety-retention routes are "
            "also excluded. Google's ZDR-aligned wording is not a no-retention guarantee. "
            "Unreviewed models do not inherit the managed-account ZDR contract."
        )
    return None
