# xAI Grok Managed ZDR

Effective 2026-11-02, the operator enabled team-wide zero data retention for
TrustedRouter's xAI account. Only TR-funded (`Credits`) endpoints inherit this
account setting through `prepaid_zero_data_retention`. BYOK credentials remain
unverified and must not inherit the managed account's privacy qualification.

This is provider-policy ZDR, not confidential compute or provider end-to-end
encryption. Grok must continue to reject Confidential and Trusted privacy floors.
Operational metadata retention is separate from prompt and output retention.

## Verification

Read-only authenticated `GET https://api.x.ai/v1/api-key` checks returned HTTP 200
and `x-zero-data-retention: true`, with the key and team not blocked or disabled:

| Credential source | Verified At (UTC) |
| --- | --- |
| GCP managed-key secret | 2026-11-02 18:38:02 |
| AWS eu-west-1 managed-key secret | 2026-11-02 18:39:43 |
| AWS eu-west-3 managed-key secret | 2026-11-02 18:39:44 |

These are credential-policy checks, not paid inference or proof of every running
gateway's loaded credentials. The encrypted Azure gateway bootstrap bundle was
not independently inspected in this check. No credentials or request content
were published. An earlier `/v1/models` check returned HTTP 200 without the ZDR
header and was inconclusive, not evidence that ZDR was disabled.

The [xAI security documentation](https://docs.x.ai/developers/faq/security)
describes ZDR as a team-wide setting applying to all API keys in that team.
Recheck credential scope when rotating keys or moving to another team; do not
infer qualification from a successful inference response alone.

## Compatibility

xAI documents that ZDR disables stored Responses history, Files/Collections,
Batch and deferred completions. Image outputs require base64 or a customer
pre-signed upload URL; the existing Grok image adapter requests base64.

Video requires `output.upload_url`. The existing gateway video adapter does not
send this field and expects a provider-hosted output URL. Enabling ZDR does not
fix that compatibility gap. Do not disable ZDR, replay paid video requests, or
claim video acceptance from these metadata changes. A compatible, reviewed
output-storage flow is separate work.
