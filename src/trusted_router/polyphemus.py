"""Polyphemus's selector contract. Generation uses ordinary model billing.

The attested gateway picks the model itself with a local open classifier
(NVIDIA's prompt task-and-complexity classifier). Nothing leaves the gateway
to choose. Strong model if the request has tools, the task type is Code
Generation, or domain knowledge is at least 0.8; otherwise the cheap model.
"""

MODEL_ID = "trustedrouter/polyphemus-1.0"
SELECT_ROUTE_TYPE = "responses.polyphemus.select"
# Selection runs inside TrustedRouter's own attested gateway, so the selector
# route's provider is TrustedRouter itself. The gateway still accepts the old
# "telluvian" authorization provider during the rollout.
SELECTOR_PROVIDER = "trustedrouter"
SELECTOR_ENDPOINT_ID = f"{MODEL_ID}@{SELECTOR_PROVIDER}/prepaid"
# Holds admitted before the provider change still name this route at settle.
LEGACY_SELECTOR_ENDPOINT_ID = f"{MODEL_ID}@telluvian/prepaid"
# TrustedRouter's in-gateway selector fee, charged on the classified first
# user message only (UTF-8 bytes / 4, rounded down, minimum one token).
SELECTOR_PROMPT_MICRODOLLARS_PER_MILLION = 50_000
LEGACY_SELECTOR_FEE_MICRODOLLARS = 1

# The model pairs the gateway selects between. Keep in sync with
# quill-cloud-proxy's local selector.
DEFAULT_STRONG_MODEL_ID = "anthropic/claude-opus-5.5"
DEFAULT_CHEAP_MODEL_ID = "deepseek/deepseek-v4.1-flash-1017"
CONFIDENTIAL_STRONG_MODEL_ID = "moonshotai/kimi-k3.1-pro"
CONFIDENTIAL_CHEAP_MODEL_ID = "deepseek/deepseek-v4.1-flash-1017"
SELECTABLE_MODEL_IDS: tuple[str, ...] = tuple(dict.fromkeys((
    DEFAULT_STRONG_MODEL_ID,
    DEFAULT_CHEAP_MODEL_ID,
    CONFIDENTIAL_STRONG_MODEL_ID,
    CONFIDENTIAL_CHEAP_MODEL_ID,
)))
