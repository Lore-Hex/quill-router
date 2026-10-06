"""Public copy for where TrustedRouter sends prompts and how long it keeps them."""

CONTENT_HANDLING_CLAIM = (
    "TrustedRouter never logs prompt or output content. Ordinary synchronous "
    "and streaming inference does not retain it. The opt-in Batch API "
    "temporarily retains enclave-encrypted artifacts for up to 30 days."
)

# catalog_privacy.endpoint_meets_privacy_requirement enforces this: the
# Confidential tier never admits the model maker's own service.
VENDOR_EXCLUSION_CLAIM = (
    'With Confidential privacy (provider.min_privacy "confidential"), prompts '
    "never reach the model vendor: a Confidential request routes only to "
    "third-party confidential-compute hosts, and fails rather than fall back "
    "to the vendor's own API."
)
