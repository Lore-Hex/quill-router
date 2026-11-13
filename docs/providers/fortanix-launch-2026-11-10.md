# Fortanix Introductory Route

## Scope And Source

Joseph authorized public launch of the existing Fortanix GLM-5.3-Flash
integration on November 10, 2026, at TrustedRouter's minimum retail price.
He confirmed that the supplier is not charging TrustedRouter, selected no
campaign spend cap, and left the promotional end date unspecified.
This is an operator-confirmed commercial arrangement, not a scraped public
Fortanix quote or a permanent supplier-price guarantee.

The same operator explicitly confirmed ZDR and a Santa Clara datacenter.
These declarations apply to this managed route. They are not independent
attestation of provider compute, per-request location, provider-wide policies,
corporate headquarters, or provider-side end-to-end encryption. Geographic
failover behavior and a per-request location pin have not been verified.

## Published Offer

- Public model: `fortanix/glm-5.3-flash`.
- Native model: `GLM-5.3-Flash`.
- Input and output: $0.01 per million tokens each, including the normal floor.
- Standard $0.000001 minimum per billed request and component rounding apply.
- Managed credits only; no BYOK or cache-discount claim.
- Introductory offer with no announced end date. Pricing may change.
- Managed-route ZDR; Santa Clara, California, US inference by operator declaration.

The existing `trustedrouter/fortanix-test` ID and its historical benchmarks
remain internal. The launch does not expand that ID's access or change its
historical reference tariff. Existing account, key and abuse controls remain.

The price-coverage gate accepts only the exact public model, native ID, route,
and tariff. New public routes or changed rates require a separate price review.
The banner disappears if the advertised route is removed, held, or repriced.
No countdown or invented expiration date is displayed. End the offer by
reviewing the supplier agreement and changing the public tariff; do not leave
the banner advertising a rate that is no longer billed.

The locally vendored provider icon is converted from the official
https://www.fortanix.com/favicon.ico, retrieved November 10, 2026. The social
preview uses the repository renderer and counts only the public model/route.

## Context Window

The public route has a **1,048,576-token total context window**, including
input and requested output. The previous 131,072 value was inherited from
the restricted test model, not Fortanix's serving configuration. The private
test model keeps its historical limit.

Verified on November 12, 2026, with the operator-approved existing provider
credential held only in memory:

- Fortanix's authenticated `https://ctf.armet.ai/v1/models` returned
  `id=GLM-5.3-Flash`, `max_model_len=1048576`.
- One bounded direct request reported 1,047,000 prompt tokens and 31 output
  tokens, returned the synthetic checkpoints at the beginning, middle, and
  end correctly, and finished with `stop`. This verifies near-limit input,
  not every token at the boundary, throughput, or long-output capacity.
- Local token counts used the publisher's tokenizer at revision
  `46a050bec13002ad29ee795c16e61cf4fa3c4a9c`, without remote code execution.
  Its count matched the provider's reported prompt usage exactly.
- An earlier 120K request returned HTTP 200 but a local response-wrapper
  parsing error made that attempt inconclusive. It was retained, not replayed.

The publisher also documents a
[1M native context](https://docs.z.ai/guides/vlm/glm-5.3-flash), but the serving
claim above rests on Fortanix's own metadata and direct inference, not solely
the native specification. This correction does not add an output-limit,
vision, tool-calling, or other capability claim, or change the tariff.
Direct provider testing is not verification of the deployed router path;
verify the public catalog and admission after the normal GCP rollout.

## Release Acceptance

Source changes, local tests, and a merged PR do not establish live serving.
Use normal control-plane release gates and verify actual customer admission,
JSON and streaming inference, billing, routing attribution, public catalog,
and the banner after deployment. Preserve concurrent release ownership.
