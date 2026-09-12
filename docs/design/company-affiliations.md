# Company directory affiliations

Sign in with TrustedRouter can disclose a sourced company-directory match under
the existing `profile` scope. This does not establish employment, eligibility,
investor endorsement, or a new identity-verification level. It is not OAuth with
YC, StartX, or an investor. Google verifies an email when used to sign in; the
directory supplies the separately sourced company association.

## Data, not code

The private Google Sheet is the editable source of truth. Never commit the
company dataset, CSV exports, or generated snapshots. Keep all public records
in the Sheet, including missing data and uncertain/retired companies. Unknown
founding years are blank, never inferred from batch or investment year.

The Companies tab has these columns:

| Column | Meaning |
|---|---|
| funding_organization | One of the supported accelerator or VC names |
| company_name | Display name from the official directory |
| directory_url | Official organization listing/profile source |
| company_url | Official linked company website |
| domain | Exact email hostname, lowercase/IDNA, without www or URL parts |
| founding_year | Sourced four-digit year, or blank |
| founding_year_source | URL supporting the founding year |
| listing_status | active, operating, public, private, or listed for usable rows |
| verified_at | Last actual evidence review, ISO 8601 UTC |
| notes | Missing data, redirects, ambiguity, review explanation |
| enabled | TRUE only after evidence/domain review; otherwise FALSE |

A listing is not proof that a domain remains owned by the original company.
Before enabling, check the official directory, the linked website, current
identity, and redirects. Hold acquired, parked, shared-email/hosting, ambiguous,
unresolved, programs/funds, and careers-only evidence for manual review. Do not
assign an acquirer's hostname to the acquired company. Never enable by name
similarity alone. Snapshot validation rejects conflicting domain ownership and
founding years. Additional reviewed exact email domains need explicit rows;
subdomains do not inherit matches.

## Publish and refresh

Export the Companies tab as CSV outside the checkout. Run from the deployed
revision with the intended cloud's normal storage configuration and identity.
The same importer supports GCP Spanner and AWS/Azure PostgreSQL-wire stores.
It does not create schema, change IAM, touch billing, or make direct DML calls.

```sh
PYTHONPATH=src python scripts/import_company_affiliations.py \
  --csv /secure/path/companies.csv --sheet-url 'https://docs.google.com/spreadsheets/d/SHEET_ID/edit'
PYTHONPATH=src python scripts/import_company_affiliations.py --status
PYTHONPATH=src python scripts/import_company_affiliations.py \
  --csv /secure/path/companies.csv --sheet-url 'https://docs.google.com/spreadsheets/d/SHEET_ID/edit' \
  --apply --expected-revision none
```

Use the exact prior revision instead of `none` on subsequent imports. A mismatch
aborts the entire transaction. Read-back must match. Deploy code before import,
then repeat the same reviewed export for every independently stored cloud.
Read-only dry runs never open a database connection. `--status` does. Protect
write access to the Sheet and database as authorization-adjacent configuration.

Data lives in `tr_entities`, kind `company_affiliation`: a `current` pointer and
immutable revision-qualified hash buckets. All buckets and the pointer publish
in one transaction. Old buckets are retained for readers of prior revisions.
Only reviewed imports advance the pointer. No user email is stored in the
directory, sent to Google, or included in enrichment logs.

Per-process cache TTL is 60 seconds. Both observations and snapshots expire
after 30 days. Re-review sources before refreshing timestamps; do not extend
old evidence by merely reimporting it. Expired/unavailable data yields no claim,
not a negative statement about the company. Imports do not run automatically
without review. Enrichment has a one-second deadline and never blocks sign-in.
Rollback by publishing a reviewed prior export with a fresh expected revision,
or publish an all-disabled export to remove claims within the cache TTL.

## Claims

`/v1/auth/userinfo` and the legacy PKCE exchange's `identity` share one builder.
The RFC token endpoint includes affiliations under `trustedrouter` only when
the grant includes `profile`. Unmatched/unverified users omit the field.
Inference-only grants never receive affiliation metadata.

```json
{
  "company_affiliations": [{
    "company_name": "Example Company",
    "funding_organization": "Y Combinator",
    "relationship": "accelerator",
    "domain": "example.com",
    "founding_year": 2020,
    "source_url": "https://www.ycombinator.com/companies/example-company",
    "checked_at": "2026-09-12T00:00:00+00:00",
    "match_method": "verified_email_domain"
  }]
}
```

Multiple organizations remain separate. `relationship` is `accelerator` for YC
and StartX, `portfolio` for VCs; it does not imply that StartX invested. Consumers
must tolerate absent claims, null years, and future fields. Fetch userinfo for
current metadata rather than treating an old token-exchange response as
permanent. Display "Company listed by Y Combinator" or "YC company email-domain
match", not "YC authenticated" or "verified YC employee".
