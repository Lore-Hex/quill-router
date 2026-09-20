# Phala First-Party Discovery

The hourly Phala pricing adapter and coverage audit used Redpill's catalog
(`api.redpill.ai/v1/models`), conflating two discovery sources. Phala now
publishes its own public catalog at
<https://inference.phala.com/v1/models>, alongside its website catalog at
<https://phala.com/models>. Its integration page documents the first-party
endpoint: <https://phala.com/confidential-ai-models>.

Both discovery paths now use the first-party URL, without credentials or
redirects. An unavailable, empty, malformed, or wholly unpriced feed fails
refresh rather than substituting Redpill's feed. Existing publication gates
and last-known-good fallback remain in place.

## Verification

On September 20, 2026, the first-party endpoint returned HTTP 200 anonymously,
with 23 rows and embedded USD/token pricing. The existing reviewed route
selection accepted five models: Kimi K3, GLM 5.1, GLM 5.2, GLM 5.3, and
GLM 5.3 Flash. Refreshing the manifest changed only its source and timestamp;
model IDs, context limits, input/output/cache prices, and route classification
were unchanged.

This is a discovery-source correction, not a runtime migration or an
attestation approval. It does not change inference hosts, broaden the
reviewed model families, or promote models to confidential/ZDR based on
website claims. Those require separate route verification.

Regression tests cover the source shared by scraper/audit/manifest, no
credential transmission, cached prices, preserved route posture, redirects,
HTTP failures, and invalid/unpriced feeds leaving the manifest untouched.
