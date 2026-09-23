"""Polyphemus's selector contract. Generation uses ordinary model billing."""

MODEL_ID = "trustedrouter/polyphemus-1.0"
SELECT_ROUTE_TYPE = "responses.polyphemus.select"
# Telluvian's direct Model Select tariff, confirmed 2026-09-23:
# https://telluvian.ai/docs/pricing (routing, prompt tokens only).
SELECTOR_PROMPT_MICRODOLLARS_PER_MILLION = 50_000
LEGACY_SELECTOR_FEE_MICRODOLLARS = 1
