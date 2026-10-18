# Web Fetch

Fetch a public web page as Markdown with `POST https://api.trustedrouter.com/v1/tools/fetch`.
Use a TrustedRouter API key with credits. BYOK is not supported.

```bash
curl https://api.trustedrouter.com/v1/tools/fetch \
  -H "Authorization: Bearer $TRUSTEDROUTER_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: my-fetch-001" \
  -d '{"url":"https://example.com"}'
```

```json
{"title":"Example Domain","content":"# Example Domain\n..."}
```

`model` is optional and defaults to `deepseek/fetch`. The only supported model
is `deepseek/fetch`. `provider` accepts the normal provider/privacy restrictions;
restrictions that cannot be satisfied fail before the URL is sent upstream.
Extra request fields, credentials embedded in URLs, non-HTTP(S) URLs, and
URLs longer than 8192 characters are rejected. Do not submit private URLs or
URLs containing access tokens. Fetched text is untrusted third-party content,
not instructions for your agent to follow.

## Billing and Limits

A successful, nonempty response costs **$0.000792** ($0.792 per 1,000 requests),
including the standard router markup on DeepSeek's $0.75 per 1,000 standalone
Fetch requests. An OAuth app's separately configured markup may also apply.
There is no token charge. Failed or empty fetches release the reservation.
A successfully completed upstream fetch remains billable if the client
disconnects. Key limits and workspace credit limits apply before forwarding.

Responses include `X-Usage-Cost` (USD), `X-Generation-Id` and `X-Request-Id`.
The generation record identifies DeepSeek and the `tools.fetch` route, with
zero input/output tokens. Successful content is returned only after settlement.

The upstream deadline is 60 seconds and the response limit is 8 MiB.
Provider rate limits apply; a rate-limit rejection returns HTTP 429.
There are no automatic upstream retries or alternate-provider fallbacks.
Repeating an idempotency key returns HTTP 409 without a second fetch or charge:
content is not retained for replay. A different URL with the same key is also rejected.
After an uncertain response, inspect the generation/usage record before retrying
with a new key, which represents a new billable operation.

## Privacy

The caller-submitted URL is forwarded to DeepSeek's standalone Fetch API.
It is not sent to a chat model. No conversation, caller authorization header,
cookies, or arbitrary request headers are forwarded. The gateway does not visit
the submitted URL itself and does not follow upstream API redirects.

The URL and returned content stay out of TrustedRouter control-plane billing
payloads, logs, broadcasts, and persistent content storage. Billing retains
content-free metadata and a keyed request fingerprint for idempotency.
This endpoint is **not ZDR or confidential inference**: DeepSeek receives the
URL and may retrieve its contents. `provider.min_privacy="zdr"`,
`provider.min_privacy="confidential"`, and the confidential-only API hostname
reject this operation. These protections do not imply DeepSeek has zero retention.

Upstream references: [Fetch API](https://api-docs.deepseek.com/api/fetch/)
and [pricing](https://api-docs.deepseek.com/quick_start/pricing/).
