# Shadow evidence rejected a short release label

## Evidence

An October 10 read of 77 day-scoped shadow-counter records found no inserted
comparison samples. Several records had comparison attempts and `worker_error`
drops. Their `router_revision` values were short release labels, including
`c07628b`, rather than complete source commits.

The Cloud Run rollout sets `TR_RELEASE` to a short commit for display and
deployment convergence checks. The shadow runtime copied that value into
evidence, whose validator correctly requires 40 lowercase hexadecimal
characters for each deployment revision. Consequently a successful comparison
could fail validation before its sample was persisted. This is separate from
the shadow Spanner deadline failures repaired in #1658.

A regression test runs a comparison through the real evidence writer with a
fake database and verifies that a sample is actually persisted. It failed on
the previous runtime because the short display label reached the validator.

## Repair

- Keep `TR_RELEASE` as the short display label.
- Supply `TR_SOURCE_REVISION` from the reviewed checkout's complete commit.
- Reject malformed source identities, checkout mismatches, and release-prefix
  mismatches before the rollout accesses cloud APIs.
- Use the complete source identity for shadow evidence. Existing local test
  fixtures with a full commit in `release` remain supported.
- Keep the evidence validator strict. Do not expand short hashes by contacting
  GitHub from a request worker or substitute the enclave repository's commit.

No settlement semantics, observer admission, privacy settings, or alert
thresholds change. Async settlement remains disabled in production.

## Production verification

After the guarded release, verify each serving control-plane revision has the
expected `TR_SOURCE_REVISION`, then use a bounded day-scoped metadata read to
confirm newly inserted comparison samples. A merged PR or a healthy HTTP
endpoint alone does not prove the evidence path is repaired. Verify the separate
Spanner deadline repair against post-release API failure metrics as well.
