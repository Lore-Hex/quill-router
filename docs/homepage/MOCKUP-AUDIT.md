# Application UI audit — September 29, 2026

Compared the current application to `nyte-design-preview/dist/trustedrouter/index.html`, the original Downloads `trustedrouter-homepage-mock/index.html` and `app.js`, and the marketing handoff SPEC, DECISIONS, OPEN_QUESTIONS and supplied top/footer screenshots. Newer user-approved art direction supersedes the old mock's typography and composition. This audit is about UI presence and implementation; it does not revalidate every marketing claim.

## Preserved from the approved pre-port design

All nine marketing modules remain: navigation, landscape hero, catalog, migration, four verification/reliability cards, pricing, customer/press proof, FAQ and footer. The request demo remains its own section below the hero. Seven FAQs, five footer groups, three catalog filters, five routing aliases, four migration tabs, OpenAI/OpenRouter switching, copy actions, search and tooltips remain. Comparison found no missing link labels in migration, verification, pricing, customers, FAQ or footer. Text in those five non-footer sections matches the pre-port HTML exactly. The approved content JSON remains byte-identical.

The original landscape PNG, responsive images, original colored lab assets and fonts are retained. The eleven mobile supporting disclosures remain in HTML, closed on phones and open on desktop. The four-font laboratory is retained as an optional local/test setting, hidden in the normal application view. Design-kit and Exchange pages remain in the original reference repository; they were not supposed to become public homepage navigation.

## Compared with the original marketing mock

| Item | Application status | Classification |
|---|---|---|
| Nine modules, four proof visuals, seven FAQs, five footer groups | Present | No missing section |
| Catalog lab icons, privacy filters, routing-promise chips | Present | Restored before the port and retained |
| Full model search and real catalog prices/counts/privacy | Connected to the application catalog; lite feed for search | Old handoff blocker resolved |
| Popular ordering | Uses the approved editorial selection; labeled Featured | No claim of measured live popularity |
| Navigation status dot and footer operational pill | Connected to `/status.json`, with freshness/error states | Restored; real public status on the remote review bridge |
| Request attestation age, token counts, served/standby and cost | Present as an explicit illustrative example | Real captured evidence remains outstanding; no fabricated live result |
| Pricing evidence line | Calculated from the current Credits routes for GLM 5.3 Flash; units, standard fee basis and exact model link included | Connected; approximately 28.6× at review, with no hardcoded rates or ratio |
| Four positioning-word interactions | Beside the below-hero request diagram; click/tap/keyboard highlight | Intentionally relocated; hero subtitle remains stable |
| Sticky navigation, serif mock fonts, greyscale logos, theme control, animated connectors | Replaced by approved normal-flow header, shared fonts, colored logos and restrained motion | Intentional design changes, not port losses |
| Optional automatic cycling through the four words | Not added | Optional mock suggestion, not an approved requirement |
| Homepage-specific analytics | Local event hooks; existing app acquisition/sign-in tracking remains | Integration work, not missing visual UI |

Scale statistics, an extra logo row, standalone routing/privacy/reliability sections, demo video, onboarding steps, supplier section and resources row were explicitly cut or merged in the marketing handoff's DECISIONS. Do not restore them as missing deliverables.

## Remaining editorial/product checks

Confirm privacy terminology across homepage and `/models`, fee and payment wording, strict-budget caveats, route-specific E2EE definitions, customer attribution and consistent API-key CTA wording. These are inherited questions, not verified defects or newly missing UI. The request and fail-closed examples must be rechecked before making current-data claims.

## Visual refinement in this pass

The phone overlay is lighter, especially below the copy in the compact-height variant. Original image/crop/foreground placement remain unchanged. Desktop ribbon bottom margin now leaves 88px at 1440×900 and about 74px at 1280×720, instead of 16px. Compact landscape keeps its existing fit. Verified 390×700, 390×844, 320×568, 1440×900 and 1280×720 without horizontal overflow; all eleven disclosures closed on phones and open on desktop. Photo/text contrast checked against the decoded responsive image and CSS gradient, with the subtitle protection adjusted after measurement. This is targeted visual QA; deferred repository release gates remain deferred.
