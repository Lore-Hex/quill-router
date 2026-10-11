## Bounded docs rollback completed — 2026-10-11

- User approved undoing the broad docs rollout and its local corrections. Restored affected detail templates, shared includes and content-layout.css to pre-rollout commit 03cfe6a9. Approved provider-routing template, navigation JS, tokens and earlier merged work preserved. `/docs` retains its requested main composition plus desktop navigation; no mobile contents bar.
- Current work saved in recovery-before-docs-rollback-2026-10-11 (working files, local patch, rollout patch and manifest). Nothing committed or pushed. Full suites remain paused. Broader docs adoption is deferred; earlier completion/approval statements below are superseded.

# Shared content layout

Provider routing is the approved reference for checklist items 14 (alignment) and 17 (phone readability). The components are opt-in and loaded by the public, homepage, console, authentication and dashboard shells. All 23 rendered documentation pages now adopt these patterns. Public landing pages, account and authentication layouts remain outside this rollout.

## Design rules

- Use boundaries to clarify meaningful groups. One outer panel often suits a reference section, but this is not a universal page template. Hubs need distinct destination groups; comparisons may need rows or bounded cards; tutorials need ordered steps. Avoid redundant nested decoration, not useful grouping. Give code a subtle background and keep executable examples comfortably wide.
- Pale headings lead; pale-blue technical labels remain subordinate. Underline actual links. Neutral body copy and 16px reference text maintain a consistent reading size.
- Documentation notes use the lavender information-note token, not live-warning colors or warning triangles. Keep the essential qualification visible in a native disclosure summary; supporting details may collapse.
- Never add visible instructional filler such as “Scroll sideways to see the full example.” Prefer sensible wrapping and recognizable, accessible controls. Keep assistive-technology labels and keyboard access.
- Technical paths may wrap at slashes/dots without altering their copied text. Preserve meaningful code formatting.

## Component contract

`content-section`, `content-prose`, `content-group`, `content-examples`, `content-example`, `content-details`, `content-note` and `content-disclosure` share typography and spacing. Definition lists keep term/description order on phones and use paired desktop columns.

`data-reference-table` enhances simple tables while retaining headers and roles. Tables with merged cells or nested tables are excluded. `data-reference-compact` specifically expects three columns: field, explanation, accepted values. Do not apply it blindly to arbitrary tables. Reference entries stack through 900px; desktop tables must retain comfortable field and value widths.

The code enhancement supplies arrow buttons only when needed, preserves keyboard scrolling, and updates on resize, font load and disclosure opening.

## Adoption and review

Documentation component adoption covers the hub and every rendered `/docs/*` guide. This is an implementation inventory, not design acceptance. The user rejected the hub composition on 2026-10-11; the docs design review is reopened. Structural fixes to overflow and wrapping do not establish improved readability. Other page families require a separate adoption review.

Agents own automated checks and parallel visual review; the user reviews design direction, not every page. Capture every changed view at appropriate widths, inspect hierarchy, grouping, readable code, spacing and expanded states, and distinguish baseline problems from regressions. Screenshot generation and zero overflow alone do not establish readability. Signed-out console captures do not establish authenticated-console coverage.

## Long-page navigation

Wrap long content in `[data-content-toc]` with a `.content-document` child. Navigation derives its labels and anchors from h2 headings, preserves existing section IDs and generates collision-safe missing IDs. At 1400px and wider it uses a sticky right rail with enough remaining width for examples; smaller screens omit section navigation to preserve reading space. The site header remains in normal flow. Current location is highlighted, navigation focuses the destination heading. Section history returns to section headings; JavaScript-disabled pages retain the readable document without generated navigation.

Reader spacing is deliberately tighter within reference entries and notes. Navigation is not a substitute for reviewing content length and reading effort.

## Compact mobile settings

For Field / Effect / Values tables, `data-reference-accordion` plus a concise `data-purpose` on every row enables native mobile disclosures through 900px. The first entry starts open; readers may open multiple entries. The original desktop table and all technical content remain intact. Enhancement requires a valid three-column table and removes cloned IDs and redundant visual cell labels. Without JavaScript the original table remains available.

Mobile and tablet layouts have no Contents bar or sticky navigation. The desktop sidebar remains visible from 1400px, with full heading labels, original anchors and active-location highlighting.

## Rollout scope

PR #1683 covers shared foundations and the complete rendered documentation category, following the approved provider-routing reference. Items 14/17 remain open for docs design correction and review as well as CI. Earlier statements that docs design review was complete are withdrawn. They are not complete site-wide.

Use one later site-adoption PR, reviewed internally in these groups rather than one PR per category:

| Review group | Representative views and risks |
| --- | --- |
| Public information and discovery | Pricing, model catalog, provider/model details, status and articles: readable tables, filters, comparison layouts and technical notes. |
| Account and authentication | Console API keys, activity, credits, settings, sign-in and consent: forms, errors, empty/loading states and authenticated behavior. |
| Homepage and conversion flows | Already-approved homepage plus shared navigation, footer and sign-in pop-up: regression checks first; migrate only patterns that actually improve these layouts. |

The documentation rollout is authorized; the later site-adoption groups still need approval. Exchange sites use their own design and are outside the TrustedRouter rollout.

For each group, agents inventory routes and states, choose representative pattern samples for user approval, capture every changed view, run structural/interaction checks, and independently inspect screenshots. Record inspected coverage and unresolved failures; do not equate generated screenshots with review. Authenticated coverage requires real authorized signed-in states. Escalate new design decisions and exceptions to the user, not routine page-by-page QA.


## Historical implementation coverage — 2026-10-10

The following records earlier checks and reported inspection. It is not current design approval: the 2026-10-11 user review found inadequate hierarchy and grouping on the hub. Keep structural evidence separate from design acceptance.

| Group | Rendered routes |
| --- | --- |
| Hub and setup | `/docs`, `/docs/agent-setup`, `/docs/migrate-from-openrouter`, `/docs/mcp` |
| Routing and model controls | `/docs/provider-routing`, `/docs/performance-routing`, `/docs/spend-controls`, `/docs/model-precision`, `/docs/prompt-caching`, `/docs/custom-models`, `/docs/user-models` |
| API guides | `/docs/batch`, `/docs/decide`, `/docs/web-search`, `/docs/video`, `/docs/notify`, `/docs/x402` |
| Evidence and operations | `/docs/tagging`, `/docs/telemetry`, `/docs/receipts`, `/docs/provider-conformance`, `/docs/evals`, `/docs/synth` |

`/docs/fusion` redirects to Synth. `/docs/llms.txt` and `/docs/llms-full.txt` are machine-readable text, not visual layouts. `/sms`, feature landing pages, catalogs and account screens are outside this category.

- Capture all 23 rendered pages at 320, 390 and 1440px. Review overlapping tiles, not only first screens. Independent reviewers inspected all 390/1440 tiles of the 22 newly migrated pages, targeted 320px risks, then rechecked fixes. Provider routing remains the approved reference; all21 of its390/1440 regression tiles were also inspected after shared refinements.
- Responsive browser audit checks all 23 pages at 320, 900, 901 and 1440px for document overflow, visual/DOM heading order, duplicate IDs, working contents anchors and absence of mobile navigation. Expand disclosures at 320px. Check every route with JavaScript disabled at 320px. The final115 route/viewport/fallback checks passed, including no remaining cramped two-column desktop code examples.
- Inspect expanded telemetry schemas and Decide comparisons at phone widths and desktop. Verify native disclosure keyboard behavior. Preserve all reference data; collapsed summaries describe their contents, while essential qualifications remain visible.
- Use paired definitions for two-column field/explanation references. Reserve compact settings disclosures for compatible three-column schemas. Comparison tables keep their actual column labels; never relabel arbitrary data as Field/Effect/Values.
- Use `content-examples-stack` for long desktop examples, and `content-code-wrap` for natural-language prompts or short endpoint paths. Do not wrap executable code indiscriminately.
- Guide links underline titles, not whole descriptive paragraphs. Shared includes opt into docs layouts with `docs_content_layout`; public callers retain their established layouts.
- Baseline comparison against live docs established the hub's 320px overflow and cramped agent URL/telemetry field tables as pre-existing. Final telemetry initial height at320 is 8,507px versus live13,656px (about38% shorter); all fields remain accessible in disclosures.
- Full local ruff/mypy/pytest suites remain paused by user instruction. Focused CSS lint, JavaScript syntax, diff checks and browser validation do not replace CI.


## Page-purpose inventory and reassessment — 2026-10-11

Shared reading components should survive a replacement page composition. Improve typography, code, tables, notes, actions and phone behavior now; avoid complete interim redesigns of pages awaiting a supplied design. `/docs` is known to be awaiting a replacement composition; replacement designs for individual `/docs/*` guides have not been specified. Public models/details, comparisons, OpenRouter-alternative, blog, vibe-coder, pricing and provider pages are in the incoming-design group, outside this docs correction.

A hub helps readers choose a destination. A tutorial gets readers through ordered actions. A reference makes a named item easy to find. A comparison keeps attributes aligned across choices. A mixed guide can use more than one of these patterns; never flatten all of them into the provider-routing layout.

This reassessment inspected 66 existing screenshot tiles: first and middle 390px tiles and the middle 1440px tile for each of the 22 detail routes below. The source was `/private/tmp/tr-docs-final-spacing` for agent setup, provider routing, batch, web search, video and prompt caching; `/private/tmp/tr-docs-final` for the remaining routes. This was triage, not full-page or expanded-state acceptance. No fresh interaction or 320px checks were run in this reassessment. Newly corrected pages require fresh screenshots. The route-specific observations below describe this pre-correction evidence.

| Route | Purpose and reusable pattern | Observed state / next review | Replacement composition |
| --- | --- | --- | --- |
| `/docs` | Hub: short quickstart, task destinations, compact essentials | User rejected previous grouping; approved bounded composition correction; fresh full review required | Expected |
| `/docs/agent-setup` | Tutorial plus copyable prompt reference | Source steps and prompts remain distinct; long alias/prompt sequences need full-page density review | Not specified |
| `/docs/migrate-from-openrouter` | Tutorial: ordered migration steps | Restore step treatment; migration flow was turned into a definition lookup; check button styling fresh | Not specified |
| `/docs/mcp` | Setup plus tool lookup | Tool list clear on desktop, tall term/description sequence on phone; review compact pairs | Not specified |
| `/docs/provider-routing` | Settings reference plus examples | Approved reference pattern; sampled sections retain hierarchy; not a universal template | Not specified |
| `/docs/performance-routing` | Concept/reference plus request example | Sampled sections remain distinct; related-guide links are readable | Not specified |
| `/docs/spend-controls` | Setup/reference with host comparison | Sampled bulk-key prose remains dense; protect essential budget qualifications; full review pending | Not specified |
| `/docs/model-precision` | Metadata field reference | Paired definitions suitable; long inline object paths still fragment on phone | Not specified |
| `/docs/prompt-caching` | How-to plus API comparison | Sampled API groups distinguish contexts; compare paths and labels rather than enforce cards everywhere | Not specified |
| `/docs/custom-models` | Setup plus product comparison | Actual comparison table retained; sampled desktop/phone relationships remain identifiable | Not specified |
| `/docs/user-models` | Workflow/reference | Eligibility, payment and request sections remain separate; signature example needs targeted phone review | Not specified |
| `/docs/batch` | Submit/poll tutorial plus contract | Sampled example and guardrails remain distinct; full submit-to-result review pending | Not specified |
| `/docs/decide` | Request tutorial plus model comparison | Sampled explanation has long prose groups; compare full question-to-result path and expanded models | Not specified |
| `/docs/web-search` | API how-to plus control reference | Sampled request/control separation works; long request uses code controls | Not specified |
| `/docs/video` | Generation tutorial plus model comparison | Model catalog lost bounded groups from main; restore compact rows or meaningful card boundaries | Not specified |
| `/docs/notify` | API workflow plus pricing/status lookup | Four short prices consume excessive phone height; compact price rows fit better than stacked definitions | Not specified |
| `/docs/x402` | Funding/retry tutorial | Sampled ordered retry steps remain recognizable; settlement code separate from boundary notes | Not specified |
| `/docs/tagging` | How-to plus attribution reference | Body/header groups retain distinct labels; sampled code readable with controls | Not specified |
| `/docs/telemetry` | Schema reference plus privacy/operations | Sampled privacy categories distinct; expanded schemas require fresh inspection after any shared change | Not specified |
| `/docs/receipts` | Verification tutorial plus evidence reference | Comparison table retained; sampled claims are tall on phone; preserve evidence limitations | Not specified |
| `/docs/provider-conformance` | Test tutorial plus failure/result reference | Adjacent prose groups collide: second label touches preceding paragraph; fix group spacing | Not specified |
| `/docs/evals` | Procedure and checklist | Sampled lists and commands separated; no composition regression identified in limited sample | Not specified |
| `/docs/synth` | Preset comparison plus parameter reference | Sampled parameter lookup suitable, long model-alias prose still dense; full comparison review pending | Not specified |

The generic mobile hero and metrics consume roughly the first screen on many guides. That markup is inherited from main, not introduced by the docs conversion; record it as a baseline reading-effort issue rather than claiming a new regression or silently redesigning every hero.

### Acceptance responsibilities

Agents own the page inventory and the detailed review. The user approves representative compositions and genuine exceptions, not every page. For each changed route record the actual screenshots inspected, widths and disclosure states, baseline comparison, concrete findings, resolution and reviewer. Do not turn a sample into a whole-page sign-off. A correction is accepted only after the changed view has been re-inspected.

Review questions: can a new reader identify the first useful action; distinguish alternatives without rereading paragraphs; find a named field or answer; and understand which qualification applies to which example? Compare those tasks with main in addition to overflow and interaction checks. Keep unresolved exceptions visible rather than marking the category complete because all routes were captured.

### Bounded corrections implemented after this triage

- `content-steps` restores migration actions as a semantic ordered sequence, with four desktop columns and a single phone column.
- `content-value-rows` keeps short labels and values adjacent on phones; Notify pricing is the first adopter. Do not use it for long descriptions.
- `content-catalog` separates individual model options with rules and responsive columns; Video is the first adopter. This preserves all model names, IDs, capabilities and destinations.
- Adjacent `content-prose` groups receive a deliberate gap so a new heading/label cannot touch the preceding paragraph.

Fresh visual inspection completed for these four bounded corrections on 2026-10-11. The reviewer inspected all 45 overlapping 390px/1440px tiles of Video, Provider conformance, Migration and Notify; all 15 Video/Conformance 320px tiles; and eight targeted Migration/Notify 320px tiles covering the changed steps and pricing. Captures are in `/private/tmp/tr-docs-bounded-final` (Video/Conformance) and `/private/tmp/tr-docs-bounded-retry` (Migration/Notify). The initial failed captures of Migration/Notify are excluded.

The review found touching conformance action buttons on phones, fixed with the existing `content-links` flexible action group. Two fresh 320px/390px action screenshots in `/private/tmp/tr-conformance-actions-final` confirmed the visible gap. Video model groups have clear separators; migration steps read as a sequence; Notify prices remain adjacent to channels at phone widths; and conformance prose groups have a deliberate gap. No further blocking visual defect was found within those inspected corrections. This review does not establish interaction, no-JavaScript or complete 23-page design acceptance; the earlier route inventory keeps the remaining limitations explicit.


### Adjacent pages awaiting supplied designs

These pages come from `top_pages_visted.png`. They are tracked here to avoid a competing interim redesign; none was changed or visually approved in this correction. Detail/article paths below identify templates, not invented complete URLs where the source image was clipped.

| Page / family | Reading pattern to retain or improve | Incoming design | Current state |
| --- | --- | --- | --- |
| `/models` | Search/filter results and comparable model data | Expected | Outside current correction |
| Model detail pages | Specifications, providers and price comparisons | Expected | Outside current correction |
| `/compare/openrouter` | Side-by-side product comparison with qualifications | Expected | Outside current correction |
| `/openrouter-alternative` | Landing-page explanation and conversion actions | Expected | Outside current correction |
| Blog article template | Article hierarchy, evidence and related reading | Expected | Outside current correction |
| `/vibe-coders` | Audience-specific landing-page flow | Expected | Listed destination; not assessed here |
| `/pricing` | Comparable rates, billing examples and qualifications | Expected | Outside current correction |
| `/docs` | Quickstart and grouped guide directory | Expected | Bounded interim correction; full reference retained |
| Provider detail pages (e.g. `/providers/cerebras`) | Capabilities, models, evidence and route comparisons | Expected | Outside current correction |

Do not infer that every guide or every instance of a template needs an individual supplied design. Adapt the shared reading elements to the incoming composition and verify representative states before broader adoption.


### Collection boundaries — approved 2026-10-11

On the docs hub, guides and SDKs use shared-divider grids with centered collection headings and left-aligned entries. They do not need a large enclosing panel. Provider-routing sections retain their approved panels: their mixed explanations and examples benefit from a topic boundary. Consistency means shared typography, spacing and controls, not identical containers across page types. This revision is isolated to docs.html and its opt-in docs-hub.css stylesheet.

For future shared changes, record the affected route/template set before editing. Compare before/after views for each affected composition and interaction state; retain a working baseline where the replacement has not demonstrated better task clarity. Separate functional checks, actual visual review and user design approval in the inventory. Unreviewed pages stay pending, not implicitly accepted because their stylesheet was updated. The previous broad docs rollout still has recorded review debt; this hub correction does not close it.


### Docs hub decision superseded — 2026-10-11

Jasmine rejected the hub redesign after preview. `/docs` now preserves main’s original composition and content, adding only desktop section navigation. Earlier hub quickstart/task-group/collection proposals in this record are not approved rollout patterns. The provider-routing reference remains separate; other docs still require the recorded review.
