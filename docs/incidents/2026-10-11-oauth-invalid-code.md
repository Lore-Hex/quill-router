# Invalid OAuth codes returned HTTP 500

## Evidence

Three Google OAuth callbacks returned HTTP 500 in europe-west4 on October 11,
2026, between 03:56:08 and 03:56:11 UTC, on revision
`trusted-router-01558-5tt`. Each submitted an explicitly invalid authorization
code. The application traceback ends in `exchange_code` calling
`response.raise_for_status()` on Google's HTTP 400 token response. The handler
did not distinguish a rejected authorization code from an unexpected failure.

These requests are separate from the Spanner shadow-audit timeout incident.
They fail before profile lookup, account creation, or session creation. The
logs do not establish who sent them or prove an ordinary user's login failed.

## Repair

Recognize only Google's `invalid_grant` and GitHub's `bad_verification_code`
on HTTP 200 or 400 token responses. GitHub can return a JSON error with HTTP
200. A typed exception maps those cases to a fixed HTTP 400 response inviting
the user to sign in again. Provider response descriptions, authorization
codes, and credentials are never copied into the response.

Configuration failures, rate limits, unauthorized client responses, and
upstream 5xx errors keep their existing failure behavior. State, host,
verified-email, and session checks are unchanged. No retries were added.

## Validation

Before the fix, all three new callback regression cases failed with unhandled
provider exceptions. Six guard cases already passed, confirming genuine
provider failures remain failures. After the fix, all 99 OAuth/console tests,
Ruff, and mypy passed. Full-suite and release verification must complete
before calling the repair deployed.
