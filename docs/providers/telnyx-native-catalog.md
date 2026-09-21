# Telnyx native catalog

Verified September 21, 2026 with read-only authenticated API requests.

## Sources

- Model identity, capabilities, limits, regions, service tiers and default prices:
  <https://api.telnyx.com/v2/ai/openai/models>
- Fallback product prices: <https://api.telnyx.com/v2/pricing/products/inference>
- Public provider profile: <https://trustedrouter.com/providers/telnyx>

Both feeds use the operator's `TELNYX_API_KEY`. The hourly price-refresh workflow
loads it from Secret Manager. Keys and private onboarding contacts are never
included in the committed catalog.

The live catalog contained 17 Telnyx-owned text-generation models and 14
third-party passthrough entries. Only the hosted text-generation entries are
eligible. All 17 had positive USD input/output rates and cached-input prices.
The six hosted models also identifiable in the product feed had matching prices.
These checks verify metadata, not successful inference or contractual privacy.

## Pricing and discovery

The models feed is authoritative and sufficient on its own when complete. Its
decimal prices are USD per million tokens, converted to integer microdollars per
million with Decimal arithmetic. New hosted model IDs are discovered without a
hand-maintained list; exact native IDs remain in `upstream_id`.

When a hosted model lacks a usable native price, the refresh consults the product
feed. That feed uses USD per **thousand** tokens and `standard` for the default
service tier. Only matching hosted IDs and constant paid rates are eligible.
Account-level free allowances are not passed through as per-request discounts.
Nonconstant volume pricing needs an explicit billing implementation, not an
assumption that volume bands are request context windows.

Priority and flex prices never replace default prices. Available upstream tiers
are retained as `provider_service_tiers` metadata only; this change does not
enable selecting those tiers through TrustedRouter. Every priced row records
its actual `pricing_source`. Models without usable prices are withheld by the
shared manifest writer. Duplicate identities, foreign currency and malformed
pricing fail closed. A failed refresh leaves the last published snapshot live.

HTML and x402 pricing are no longer runtime dependencies. Public policy, terms,
DPA, subprocessors, trust-center, locality, status and support links remain on
the provider page. Regional availability is not a residency guarantee, and a
generic trust-center URL does not establish ZDR, TEE or end-to-end attestation.

## Verification

Run `uv run pytest -q tests/test_telnyx_pricing.py tests/test_provider_branding.py`.
Tests cover hosted-only discovery, USD scaling, currency/tier rejection, cached
input, native price precedence, cleared output limits, new and retired model
IDs, duplicate IDs, and holding unpriced routes out of the catalog.
