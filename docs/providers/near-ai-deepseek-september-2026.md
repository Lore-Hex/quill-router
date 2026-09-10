# NEAR AI DeepSeek V4 Flash retirement

NEAR's customer notice schedules `deepseek-ai/DeepSeek-V4-Flash` for retirement
on **September 17, 2026 at 13:00 UTC**. Its recommended replacement is
`z-ai/glm-5.3-flash`.

TrustedRouter keeps the existing NEAR route for `deepseek/deepseek-v4-flash`
until that exact cutoff. The shared lifecycle policy then excludes it from
new routing, catalog ingestion, discovery normalization, and refreshed price
indexes, including stale results. Other providers serving DeepSeek V4 Flash
are unaffected. Existing authorized requests retain their normal settlement
path; there is no retroactive billing change.

The suggested GLM model is a migration choice, not an automatic substitution
for a DeepSeek request. This release does not add that NEAR workload to the
enclave's verified direct-endpoint policy or reuse DeepSeek's attestation
pins. Confidential routing continues to fail closed if no eligible route
exists. The separate NEAR GLM 5.1/5.2 retirement remains September 11 at
13:00 UTC.

Boundary tests cover the microsecond before and the exact cutoff, a live
process without catalog reload, provider isolation, stale feeds with and
without prices, and the migration annotation. Six new cases failed before
the policy and metadata were added. Full post-cutover CI also verifies that
this scheduled removal cannot invalidate tests that assumed NEAR's DeepSeek
route existed forever.
