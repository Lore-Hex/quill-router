# Provider configuration incidents, October 3, 2026

## Evidence

The configuration alerts added in PR #1504 exposed sustained failures that
were excluded from provider uptime. That exclusion did not make the routes
safe for customer selection. Keep the alerts and their thresholds unchanged.

Authenticated diagnostics using the deployed operator secrets confirmed:

* Crusoe: both `/models` and `/chat/completions` return HTTP 401,
  `Authentication failed`.
* SambaNova: `/models` returns HTTP 200, but a tiny Llama inference request
  returns HTTP 401, `invalid_api_key`. Catalog success does not validate an
  inference credential.
* StreamLake: `kat-coder-pro-v2`, `kat-coder-air-v2.5`, and
  `kat-coder-pro-v2.5` each return HTTP 400, `UnavailableModel`, explicitly
  stating that the model is deprecated.

The local keyfile matches each deployed secret. Uploading the same values
again cannot repair these authentication failures. No secrets are included
in this report.

Bounded ClickHouse reads confirmed the same errors in recent synthetic
samples. No non-synthetic activity-generation records were found for these
providers, Privatemode, or NEAR in the six-hour diagnostic window. This does
not prove that there were no rejected requests before settlement.

## Containment

* Block Crusoe and SambaNova **Credits** routes at catalog construction using
  reviewed provider-account holds. Fresh discovery and stale price fallback
  cannot clear them. Do not block a customer's independent BYOK credential.
* Register the three exact StreamLake models as retired for both usage types.
  The October 3 20:00 UTC enforcement timestamp records this incident's
  confirmation, not a claimed upstream retirement announcement date.
  Do not silently substitute other weights or affect other providers.
* Keep the separate provider-health checks strict. A held provider is not
  repaired simply because customer routing has been protected.

## Recovery And Remaining Work

Renew Crusoe and SambaNova inference credentials in their provider accounts,
update the managed secrets through the normal deployment path, and verify
real inference before removing the corresponding prepaid hold. Never use
`GET /models` alone as the recovery gate.

Privatemode's three encrypted startup probes and normal production streaming
requests succeeded during this investigation. NEAR GLM-5.3-Flash also returned
a complete production stream after the reviewed attestation-policy release;
recent stored NEAR samples include successes after the last observed policy
rejection at 17:33 UTC. Neither attestation check was bypassed.

The receipt-key collector's recent scheduled executions returned HTTP 200.
Its transient-read retry fix was already merged in PR #1504.

Two API-contract warning groups are separate from these provider incidents.
An exact request trace confirmed a 400 before authorization and skipped
settlement. Older telemetry retained only `parameter=other`, so its exact
rejected field cannot be reconstructed. Preserve the warnings; do not infer
the field or claim the compatibility problem was repaired from this evidence.

## Verification

Six focused regression cases failed before the change, then passed with it.
They cover prepaid-only holds, independent BYOK and other providers, explicit
hold removal, exact StreamLake IDs and enforcement boundary, and stale price
fallback. Catalog contract tests include lifecycle and account holds without
relaxing the separate live provider-health gate.
