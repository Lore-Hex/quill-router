# Adding a Token Exchange city or region

Use the approved New York page and the shared implementation in this directory
as the design baseline. Dubai and Riyadh illustrate regional adaptations. A new
market extends this system; it does not start a new page design. This guide is
tracked so developers and coding agents receive it with the repository.

## Start with the current design

Read [README.md](README.md), applicable agent instructions, and any available
local handoff. Inspect the branch, working tree and diff before editing; preserve
other work and reference assets. Open New York and a reviewed adaptation in the
current preview. If a proposed shared change has not been reviewed, do not assume
it is the approved baseline simply because it is the newest code.

Reuse `template.html`, `exchange.css` and `exchange.js`. Keep shared typography,
spacing, CTAs, motion and page structure. In particular, preserve:

- The explicit city or region in the hero, desktop-only founder values, mobile
  hero spacing and foreground reveals.
- One market directory, current-market highlighting in header and footer, and
  the mobile backdrop. Outside taps close both menus without activating the
  content underneath.
- Working back-to-top, reduced-motion behavior and accessible controls.
- The lighter footer, Email us icon, TrustedRouter homepage link and full
  Delaware operator line. Do not restore the removed Security & Legal section.

## Add the market data and regional copy

`markets.json` is the source of per-market content. Copy the structure of a
reviewed market and adapt these fields:

| Fields | Purpose and constraints |
| --- | --- |
| `slug` | Unique lowercase, hyphenated identifier; also the preview directory and social-image suffix. |
| `name`, `region`, `scope` | Brand, short directory label, and `city`, `region` or `global`. |
| `domain`, `aliases` | Approved canonical domain and aliases; no duplicates across markets. Domain ownership and launch readiness must be established separately. |
| `headline`, `headline_accent` | Explicit market headline and an exact substring for emphasis. Check long names on mobile and social cards. |
| `lead`, `focus` | Specific audience description and workload heading. The lead also supplies metadata. |
| `sectors` | Exactly three title/body pairs for the existing workload cards. |
| `seller` | Relevant supplier requirements in the existing supplier section. |
| `buyer_heading`, `buyer_copy` | Preserve the approved shared buying message unless a verified requirement needs a change. |
| `evidence_snapshot` | A reviewed local snapshot whose scope matches the displayed claims. |

Some records retain `intro`; the current template does not display it. Useful
details belong in rendered fields, not only in unused JSON.

Consult earlier market copy for useful language needs, document types,
industries and procurement requirements, then rewrite them in the current voice.
Keep concrete local substance in the three workload cards or supplier copy.
For example, Japanese business and technical documents and manufacturing
workloads are useful Tokyo details; Arabic/English documents and energy
workloads are useful Riyadh details. Describe what buyers should evaluate, not
unverified capabilities or performance guarantees.

Do not simply swap city names, restore generic old paragraphs, add SEO filler or
invent sections. Preserve unique, useful rendered copy and check the generated
title, description, canonical URL, structured data and sitemap. Regional English
copy is not a translated site or a promise of search ranking.

## Bind evidence to what it actually measures

Inspect `refresh_evidence.py`, `evidence.py`, existing snapshots and repository
routing/configuration. Check the actual public status and release/attestation
sources referenced by the applicable refresh profile. Starting points include
[GCP status](https://trustedrouter.com/status.json),
[GCP release](https://trustedrouter.com/trust/gcp-release.json),
[Azure status](https://azure.trustedrouter.com/status.json) and
[Azure release](https://trust.trustedrouter.com/trust/azure-release.json).
Recheck these sources when establishing a new binding; old copy is not evidence.

Match component IDs, cloud/platform, origin and scope. Geography alone cannot
establish routing or local processing. Never rename a US East or Dubai chart to
the new city. Where only shared GCP evidence is established, use the shared
snapshot and accurate Shared GCP / Canonical API / Model Inference labels.
Do not imply city-level uptime or residency. If evidence is insufficient, record
the gap for reviewers and keep public claims within the verified scope; do not
automatically add a speculative “no evidence found” FAQ.

Keep these concepts distinct in copy and FAQs:

- **E2EE:** encryption properties of the particular route.
- **Retention/ZDR:** provider data-handling policy.
- **Confidential inference:** execution protection and its verification evidence.

A release document or uptime chart does not establish all three for every model.
Do not infer local offices, certifications, customer endorsements or compliance
from market branding. Keep eligibility qualifications where required. If adding
a new evidence profile, cover its selection and labels in `test_evidence.py`.
Snapshot refresh is an explicit step; the build does not fetch live evidence.

## Create the matching social image

Use `social-card.html` and `social.py`: the approved card uses the shared hero
colors, typography, brand and market headline. Capture the rendered HTML rather
than introducing unrelated artwork or capturing an arbitrary section of the
landing page. The deliverable is a real **1200 × 630 PNG** at
`social-images/og-<slug>.png`.

The normal build requires that PNG, so bootstrap a new market in this order:

1. Before adding its JSON record, build the existing markets into a temporary
   capture directory to populate the shared CSS, fonts and mark.
2. Add the market record, then run `social.py` against that same directory. It
   creates `social-<slug>/index.html` without requiring the new PNG.
3. Serve the directory, open the new social page at exactly 1200 × 630, wait for
   fonts to load and capture the viewport. Check line wrapping, clipping, brand
   and headline. A screenshot returned as JPEG must be converted to PNG; changing
   its extension is insufficient.
4. Save the reviewed PNG at the source path above, then build into a separate
   clean directory. The build copies it into `assets/og-<slug>.png` and renders
   the page's OG reference.

```sh
# Step 1: run before adding the new markets.json record.
python3 sites/token-exchange/build.py --output /tmp/token-exchange-social-capture
# Step 2: run after adding the record.
python3 sites/token-exchange/social.py --output /tmp/token-exchange-social-capture
python3 -m http.server 8089 --bind 127.0.0.1 --directory /tmp/token-exchange-social-capture
```

If the record already exists and its PNG is missing, use a previously successful
build's shared assets for the capture directory. Do not commit another city's
image as a placeholder. Keep any social-only abbreviation in `social.py` (the
U.S. card is an example), preserving the landing-page copy. Capture pages,
galleries and review archives are temporary artifacts, not production output.

## Review and hand off

- [ ] Regional details are visible in the page, rewritten in the approved voice;
  no unsupported local-processing or privacy claims were introduced.
- [ ] Snapshot source, component and labels agree; FAQs remain accurate.
- [ ] Social PNG is visually reviewed, 1200 × 630, and present in a clean build;
  the page's OG URL points to it.
- [ ] Run the site unit tests and build using README's commands. Run the browser
  harness where available; report actual blockers without claiming it passed.
- [ ] Review the full page at desktop, 390px and 320px: scrolling, reveals and
  motion phases, reduced motion, menus/outside taps, highlighting, FAQ, CTAs,
  footer and back-to-top. Check overflow, images and long-name wrapping.
- [ ] Spot-check New York and Dubai for regressions, plus any other market
  affected by a shared change. Directory growth affects every page.
- [ ] Run applicable repository checks before merge according to repository
  instructions. Site tests are not a substitute for those checks. Verify actual
  CI results rather than assuming a push ran them.
- [ ] Record copy retained/omitted, evidence sources and verification in the
  available local handoff. Keep local design notes and reference assets local;
  never force-add ignored files. Report validation separately from durable PR
  descriptions so changing CI status does not make the description stale.

Adding a JSON record or merging a PR is not proof that the site is live. Review
the current deployment workflows and README launch runbook; a new domain may
also need DNS, routing and certificates. Publishing is a separate authorized
operation, followed by public HTTPS, asset and redirect checks. Do not deploy a
temporary capture directory or assume the control-plane deploy publishes these
static sites.

The site workflow, `.github/workflows/token-exchange-sites.yml`, runs site tests
and a static build; it does not publish. The control-plane `deploy.yml` is a
different workflow. For existing hosting, README describes a content-only GCS
sync; new host infrastructure uses the inventory/DNS/publish runbook. Reinspect
the workflows before release in case that automation has changed.
