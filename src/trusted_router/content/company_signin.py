"""Developer copy for company-aware sign-in; no company directory data."""

import json

from trusted_router.company_affiliations import SOURCE_HOSTS

COMPANY_SIGNIN_PAGES = {
    "sign-in-as-ycombinator": "Y Combinator",
    "sign-in-as-startx": "StartX",
    "sign-in-as-vc": "VC-backed",
}
SIGNIN_DOCS = "https://github.com/Lore-Hex/quill-router/blob/main/docs/sign-in-with-trustedrouter.md"


def company_signin_context(slug: str) -> dict[str, object]:
    organization = COMPANY_SIGNIN_PAGES[slug]
    is_vc = slug == "sign-in-as-vc"
    firms = [name for name in SOURCE_HOSTS if name not in {"Y Combinator", "StartX"}]
    allowed = firms if is_vc else [organization]
    allowed_json = json.dumps(allowed, indent=2)
    allowed_setup = f"\nconst allowedOrganizations = new Set({allowed_json});\n" if is_vc else ""
    match_rule = (
        "allowedOrganizations.has(claim.funding_organization)"
        if is_vc else f'claim.funding_organization === "{organization}"'
    )
    button_label = "Sign in as VC-backed" if is_vc else f"Sign in with {organization}"
    asset_slug = slug.removeprefix("sign-in-as-")
    attribution = "Backed by TrustedRouter / Google"
    directory_label = "the listed venture firms" if is_vc else organization
    example_organization = allowed[0]
    example_source = (
        f"https://{SOURCE_HOSTS[example_organization][0]}/" if is_vc
        else "https://www.ycombinator.com/companies" if organization == "Y Combinator"
        else "https://web.startx.com/community"
    )
    button_assets = []
    for theme in ("light", "dark"):
        path = f"/static/sign-in/{asset_slug}-{theme}.svg"
        button_assets.append({
            "theme": theme,
            "path": path,
            "embed": f'''<a href="/auth/trustedrouter">
  <img
    src="https://trustedrouter.com{path}"
    width="360" height="88"
    style="max-width: 100%; height: auto"
    alt="{button_label}. {attribution}."
  >
</a>''',
        })
    return {
        "organization": organization,
        "is_vc": is_vc,
        "directory_label": directory_label,
        "listing_label": "venture-firm portfolio listings" if is_vc else f"{organization} listings",
        "button_label": button_label,
        "button_attribution": attribution,
        "button_assets": button_assets,
        "guide_tabs": [
            {"slug": path, "label": "VCs" if path == "sign-in-as-vc" else name}
            for path, name in COMPANY_SIGNIN_PAGES.items()
        ],
        "guide_slug": slug,
        "firms": [{"name": name, "url": f"https://{SOURCE_HOSTS[name][0]}/"} for name in firms],
        "signin_docs": SIGNIN_DOCS,
        "agent_prompt": f'''Add company-aware Sign in with TrustedRouter to this app for {organization} companies.

Read {SIGNIN_DOCS} and reuse this app's auth/session conventions and the official TrustedRouter SDK where compatible.

Register the app through /v1/oauth/apps using a signed-in TrustedRouter console session and an exact redirect URI. Use authorization-code OAuth with PKCE S256 and fresh, single-use state:
Authorize: https://trustedrouter.com/v1/oauth/authorize
Token exchange: https://trustedrouter.com/v1/oauth/token
Current profile: https://trustedrouter.com/v1/auth/userinfo
Request scope=profile; add inference only if this app needs model calls. Do not buy credits or enable a markup automatically.

Use the reusable button image https://trustedrouter.com/static/sign-in/{asset_slug}-light.svg (or -dark.svg), labeled "{button_label}" and "{attribution}". Link it to /auth/trustedrouter in this app and implement that route to start the registered OAuth flow with fresh state and PKCE; the image itself cannot authenticate anyone. Use accessible alt text and a responsive 360 by 88 image. Explain that the backing line describes TrustedRouter authentication with Google sign-in, not endorsement by Google or official authentication operated by {directory_label}.

Validate state, exchange the code, and fetch userinfo server-side. Use data.sub as the stable identity, not email. Only accept a match when data.email_verified === true and data.company_affiliations contains an exact funding_organization match to one of {json.dumps(allowed)} and match_method === "verified_email_domain". Display the returned company name, domain, organization, sourced founding_year (null means unknown), and source link. Never infer a founding year.

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
{allowed_setup}
const company = data.email_verified === true
  && Array.isArray(data.company_affiliations)
  ? data.company_affiliations.find((claim) =>
      {match_rule}
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
                    "funding_organization": example_organization,
                    "relationship": "portfolio" if is_vc else "accelerator",
                    "domain": "example.com",
                    "founding_year": None,
                    "source_url": example_source,
                    "checked_at": "2026-09-12T00:00:00+00:00",
                    "match_method": "verified_email_domain",
                }],
            }},
            indent=2,
        ),
    }
