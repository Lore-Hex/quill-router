# Public CA regression fixtures

Certificate-only fixtures from https://emudhra.we-have-a-problem.com/chains.zip,
retrieved 2026-10-20. No private keys. Covers six compromised TLS intermediate
keys and both trust paths (emSign and the AAA Certificate Services cross-sign).

Tests freeze time within validity, check each intermediate signature, and
require a trust-chain failure so expiration or malformed fixtures cannot pass.
Incident: https://bugzilla.mozilla.org/show_bug.cgi?id=2083449.
