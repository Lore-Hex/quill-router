"""Public copy for TrustedRouter's prompt and output retention boundary."""

CONTENT_HANDLING_CLAIM = (
    "TrustedRouter never logs prompt or output content. Ordinary synchronous "
    "and streaming inference does not retain it. The opt-in Batch API "
    "temporarily retains enclave-encrypted artifacts for up to 30 days."
)
