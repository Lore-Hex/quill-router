"""Developer copy for company-aware sign-in; no company directory data."""

import json

COMPANY_SIGNIN_PAGES = {
    "sign-in-as-ycombinator": "Y Combinator",
    "sign-in-as-startx": "StartX",
}
SIGNIN_DOCS = "https://github.com/Lore-Hex/quill-router/blob/main/docs/sign-in-with-trustedrouter.md"


def company_signin_context(slug: str) -> dict[str, str]:
    organization = COMPANY_SIGNIN_PAGES[slug]
    return {
        "organization": organization,
        "signin_docs": SIGNIN_DOCS,
        "agent_prompt": f'''Add company-aware Sign in with TrustedRouter to this app for {organization} companies.

Read {SIGNIN_DOCS} and reuse this app's auth/session conventions and the official TrustedRouter SDK where compatible.

Register the app through /v1/oauth/apps using a signed-in TrustedRouter console session and an exact redirect URI. Use authorization-code OAuth with PKCE S256 and fresh, single-use state:
Authorize: https://trustedrouter.com/v1/oauth/authorize
Token exchange: https://trustedrouter.com/v1/oauth/token
Current profile: https://trustedrouter.com/v1/auth/userinfo
Request scope=profile; add inference only if this app needs model calls. Do not buy credits or enable a markup automatically.

Label the button "Continue with TrustedRouter" with nearby text "Check your {organization} company email". Explain that users can sign in to TrustedRouter with Google using their company email.

Validate state, exchange the code, and fetch userinfo server-side. Use data.sub as the stable identity, not email. Only accept a match when data.email_verified === true and data.company_affiliations contains funding_organization === "{organization}" and match_method === "verified_email_domain". Display the returned company name, domain, organization, sourced founding_year (null means unknown), and source link. Never infer a founding year.

Keep ordinary sign-in working when there is no match. Show "Company affiliation not confirmed" and offer retry or manual review for company-only benefits. A directory match is not proof of employment or official {organization} authentication. Never trust browser-supplied claims for access decisions; re-fetch current claims on the server before company-only benefits.

Use secure server sessions and encrypted token storage, or platform secure storage for native apps. Never put tokens in URLs, logs, analytics, or localStorage. Handle denied consent, invalid state, token errors, missing claims and revoked access. Add tests for matching, non-matching, unverified and missing-domain cases. Do not replace or remove existing login methods.''',
        "match_code": f'''// Server-side, after the OAuth code exchange.
// accessToken comes from TrustedRouter, not a browser claim.
const response = await fetch(
  "https://trustedrouter.com/v1/auth/userinfo",
  {{
    headers: {{ Authorization: `Bearer ${{accessToken}}` }},
    cache: "no-store",
  }},
);
if (!response.ok) throw new Error("TrustedRouter profile unavailable");
const {{ data }} = await response.json();

const company = data.email_verified === true
  && Array.isArray(data.company_affiliations)
  ? data.company_affiliations.find((claim) =>
      claim.funding_organization === "{organization}"
      && claim.match_method === "verified_email_domain")
  : undefined;

// No match does not prevent ordinary sign-in.
const userId = data.sub;
const companyContext = company ?? null;''',
        "response_json": json.dumps(
            {"data": {
                "sub": "usr_example",
                "email": "person@example.com",
                "email_verified": True,
                "company_affiliations": [{
                    "company_name": "Example Company",
                    "funding_organization": organization,
                    "relationship": "accelerator",
                    "domain": "example.com",
                    "founding_year": None,
                    "source_url": (
                        "https://www.ycombinator.com/companies"
                        if organization == "Y Combinator"
                        else "https://web.startx.com/community"
                    ),
                    "checked_at": "2026-09-12T00:00:00+00:00",
                    "match_method": "verified_email_domain",
                }],
            }},
            indent=2,
        ),
    }
