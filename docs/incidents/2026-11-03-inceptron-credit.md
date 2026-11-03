# Inceptron operator credit exhausted

Six production route groups return HTTP 402 with `prepaid_credit_exhausted`:
GLM 5.3 Flash, GLM 5.3, GLM 5.2, Kimi K2.7 Code, Kimi K2.6, and DeepSeek
V4 Flash 0731. Latest observed reports were at 2026-11-03T01:01:27Z.

The provider still advertises model metadata, so a successful catalog refresh
cannot prove our account can pay for inference. Public GLM 5.3 Flash endpoints
still included Inceptron prepaid before this repair.

Use the existing prepaid-only operator hold. It blocks new managed admissions,
survives successful discovery, leaves other providers and customer credentials
alone, and preserves the last tariff for already-authorized settlements. It
does not fund the account or hide the underlying incident.

Recovery requires operator funding followed by a small, provider-pinned
usage-accounted canary. Remove only the Inceptron hold through normal CI and
deployment after that evidence exists. Do not clear it merely because
`GET /models` works. No payment was made during this repair.
