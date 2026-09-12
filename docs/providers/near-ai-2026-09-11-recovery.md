# NEAR AI recovery: September 11, 2026

## Incident

- The authenticated catalog still worked, but four old direct endpoint registry
  entries had no prices: Gemma 4 31B, GPT OSS 120B, Qwen 3.6 27B and Qwen 3.5 122B.
  The parser rejected the entire provider instead of holding those rows.
- The last successful manifest was August 28 at 05:52:57 UTC. Its 14-day deadline
  expired September 11 at 05:52:57 UTC, correctly excluding all routes.
- NEAR changed deployment identities and has multiple GLM 5.3 Flash workloads
  behind one domain. An exact single-deployment policy could not cover the pool.
- Independent Intel PCS checks rejected the three Qwen endpoints' TDX module
  with `OutOfDate`. The DeepSeek direct endpoint later stopped completing TLS.
- Route-health evaluation discarded every 502 and timeout as transient. Go
  enclave failures were logs, not Python exceptions, so no Sentry issue fired.
  Pricing/coverage failures were tracked in GitHub, not Sentry.

## Fixes and limits

- Missing prices/domains are explicit per-model holds with no stale-price route.
  Healthy priced models can refresh independently. Empty or malformed catalogs
  still fail. The mass-prune guard and security holds remain in force.
- GLM 5.3 Flash is enabled only with the companion enclave policy release.
  Both observed deployment identities passed full CPU, GPU, nonce/TLS binding,
  and reviewed deployment evidence checks; three real same-connection streaming
  PONG calls passed locally (3.65, 7.23 and 4.23 seconds).
- A final review found missing quote boot-register comparisons. Publication is
  gated on the follow-up enclave fix: independently derived dstack firmware,
  VM, kernel and initrd pins, plus runtime-event replay, on both CPU quotes.
  Both GLM pool members pass these additional cryptographic checks.
- Qwen TCB failures and DeepSeek TLS failures remain operator holds. A successful
  pricing refresh cannot clear them. No ordinary/unattested fallback is used.
- Sustained availability failures (six consecutive probes spanning at least
  30 minutes, newest within six hours) page separately from structural failures.
  They never enter the remediator's quarantine decisions.
- Partial degradation also alerts: at least 12 probes spanning 30 minutes in
  the last 24 hours, at least four failures and a 25% failure rate, a fresh
  sample within 30 minutes, and no six-probe healthy recovery. This covers
  sustained 75% provider uptime without calling it a total outage. The window
  accommodates hourly route probes, without increasing the bounded query size.
- The scheduled route-health pass alerts 48 hours before catalog expiry, grouped
  by provider. Both new alert paths contain metadata only, no provider payloads.

## Provider follow-up

NEAR must update the TDX module on the hosts serving Qwen 3.6 35B, Qwen 3.8 27B,
and Qwen3-VL 30B to a state Intel reports as `UpToDate`, and restore the direct
DeepSeek TLS endpoint. Recheck every pool member before removing holds.

Attested software changes require reviewed release pins; catalog discovery is
not authority to trust new code. The release pins the deployment action history
as well as the compose/OS identity so a registrar-only update cannot masquerade
as verification of all inference services. An upstream action-history change
fails closed and needs review, even when it is operationally benign.

Local Claude CLI Opus review was attempted, but its OAuth session was expired.
No API-key-based Claude fallback was used.

The alert paths are covered by tests, but direct Sentry delivery verification
was blocked by the operator identity's missing access to the Sentry DSN secret.
No secret-access permission was granted and no Sentry email-delivery claim is
made. The existing production synthetic job uses its own runtime secret binding.
