# Arcee and Phala route alerts, October 25, 2026

## Arcee: inaccessible DeepSeek V4 Flash

Sentry QUILL-ROUTER-CB (156008449) reported six failed rotation probes.
Production benchmark metadata records HTTP 404 `not_found_error` for
`deepseek/deepseek-v4-flash`, whose upstream ID is
`deepseek/deepseek-v4-flash-latest`.

An authenticated direct probe on October 25 reproduced HTTP 404 with
`code=model.not_accessible`. The same account's current `/models` response
omits the old Flash ID and lists V4.1 Flash and V4.1 Flash 1017. Existing
production V4.1 Flash requests succeeded, including at 13:20 UTC.

The committed manifest still enabled the canonical Flash row. An older row
with the native `-latest` ID was already tombstoned; that does not disable a
separate canonical row. The latest hourly refresh (Actions run 40183293242)
fetched Arcee successfully but did not publish the catalog because other
provider discovery coverage checks blocked publication. This repair does not
weaken those checks or remap a request to a different model.

Disable the canonical route and hold both historical IDs in Arcee's refresh
adapter. Preserve other Arcee models and other providers of the requested
model. Removal of the hold requires a reviewed successful inference canary,
not mere reappearance in `/models`.

Regression tests cover effective routing, manifest/hold consistency, other
route availability, and a relisted feed that must not clear the hold. Both
new assertions failed on the previous configuration before the repair.

## Phala: intermittent first-token timeouts

Sentry QUILL-ROUTER-CC (156035779) reported 4/12 failures in its 24-hour window.
The four failed synthetic records were all `TimeoutError` at approximately
45 seconds, at 15:31, 17:14, and 19:37 UTC October 24 and 03:59 UTC October 25.
They were not invalid-attestation or authentication failures.

Matched enclave logs show upstream rate limits at 17:13 UTC and responses
that ultimately completed after 67.9, 155.9, and 94.6 seconds. Early upstream
bytes are not proof of a usable first token. The synthetic deadline correctly
reported these slow calls instead of accepting heartbeats as token flow.
An isolated upstream 5xx at 21:07 UTC recovered on the existing retry.

Six subsequent rotation probes, from 05:25 through 14:18 UTC October 25,
passed. A direct fixed-prompt streaming check using the runtime's
`api.redpill.ai/v1` endpoint and existing Phala confidential-tier credential
also passed: first token 1.44 seconds, completion 11.036 seconds, `stop`, PONG.
The separate discovery endpoint is not the runtime credential's endpoint.

This is current recovery, not proof that upstream capacity is permanently
repaired. Keep Phala routable, keep its degradation alert open, and preserve
the existing timeout, retry limits, privacy classification, and billing.
Do not quarantine an intermittently healthy route or loosen alarms merely
to remove the alert. Phala should investigate rate limits and long token
stalls for `z-ai/glm-5.2` in the times above.

## Release verification

After the normal release completes, check both public catalog surfaces:
`https://api.trustedrouter.com/v1` and `https://trustedrouter.com/v1`.
Arcee must be absent from V4 Flash endpoints and present for V4.1 Flash and
Kimi K3. Other providers of V4 Flash must remain available. Resolve CB only
after that verification. CC remains an upstream reliability issue.

No customer prompts, outputs, credentials, credit balances, or monitoring
thresholds were changed or included in this evidence.
