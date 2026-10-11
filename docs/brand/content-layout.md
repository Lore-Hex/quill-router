# Shared content layout

Provider routing is the approved reference for checklist items 14 (alignment) and 17 (phone readability). The components are opt-in and loaded by the public, homepage, console, authentication and dashboard shells. All 23 rendered documentation pages now adopt these patterns. Public landing pages, account and authentication layouts remain outside this rollout.

## Design rules

- One outer panel per meaningful section. Group examples with headings and whitespace; give code a subtle background, without nested bordered cards.
- Pale headings lead; pale-blue technical labels remain subordinate. Underline actual links. Neutral body copy and 16px reference text maintain a consistent reading size.
- Documentation notes use the lavender information-note token, not live-warning colors or warning triangles. Keep the essential qualification visible in a native disclosure summary; supporting details may collapse.
- Never add visible instructional filler such as “Scroll sideways to see the full example.” Prefer sensible wrapping and recognizable, accessible controls. Keep assistive-technology labels and keyboard access.
- Technical paths may wrap at slashes/dots without altering their copied text. Preserve meaningful code formatting.

## Component contract

`content-section`, `content-prose`, `content-group`, `content-examples`, `content-example`, `content-details`, `content-note` and `content-disclosure` share typography and spacing. Definition lists keep term/description order on phones and use paired desktop columns.

`data-reference-table` enhances simple tables while retaining headers and roles. Tables with merged cells or nested tables are excluded. `data-reference-compact` specifically expects three columns: field, explanation, accepted values. Do not apply it blindly to arbitrary tables. Reference entries stack through 900px; desktop tables must retain comfortable field and value widths.

The code enhancement supplies arrow buttons only when needed, preserves keyboard scrolling, and updates on resize, font load and disclosure opening.

## Adoption and review

Documentation adoption covers the hub and every rendered `/docs/*` guide. The hub overflow, fragmented agent-setup URL columns and split telemetry field names are addressed. Other page families require a separate adoption review.

Agents own automated checks and parallel visual review; the user reviews design direction, not every page. Capture every changed view at appropriate widths, inspect hierarchy, grouping, readable code, spacing and expanded states, and distinguish baseline problems from regressions. Screenshot generation and zero overflow alone do not establish readability. Signed-out console captures do not establish authenticated-console coverage.

## Long-page navigation

Wrap long content in `[data-content-toc]` with a `.content-document` child. Navigation derives its labels and anchors from h2 headings, preserves existing section IDs and generates collision-safe missing IDs. At 1400px and wider it uses a sticky right rail with enough remaining width for examples; smaller screens omit section navigation to preserve reading space. The site header remains in normal flow. Current location is highlighted, navigation focuses the destination heading. Section history returns to section headings; JavaScript-disabled pages retain the readable document without generated navigation.

Reader spacing is deliberately tighter within reference entries and notes. Navigation is not a substitute for reviewing content length and reading effort.

## Compact mobile settings

For Field / Effect / Values tables, `data-reference-accordion` plus a concise `data-purpose` on every row enables native mobile disclosures through 900px. The first entry starts open; readers may open multiple entries. The original desktop table and all technical content remain intact. Enhancement requires a valid three-column table and removes cloned IDs and redundant visual cell labels. Without JavaScript the original table remains available.

Mobile and tablet layouts have no Contents bar or sticky navigation. The desktop sidebar remains visible from 1400px, with full heading labels, original anchors and active-location highlighting.

## Rollout scope

PR #1683 covers shared foundations and the complete rendered documentation category, following the approved provider-routing reference. Items 14/17 are complete for docs implementation and browser review; merge readiness still requires CI. They are not complete site-wide.

Use one later site-adoption PR, reviewed internally in these groups rather than one PR per category:

| Review group | Representative views and risks |
| --- | --- |
| Public information and discovery | Pricing, model catalog, provider/model details, status and articles: readable tables, filters, comparison layouts and technical notes. |
| Account and authentication | Console API keys, activity, credits, settings, sign-in and consent: forms, errors, empty/loading states and authenticated behavior. |
| Homepage and conversion flows | Already-approved homepage plus shared navigation, footer and sign-in pop-up: regression checks first; migrate only patterns that actually improve these layouts. |

The documentation rollout is authorized; the later site-adoption groups still need approval. Exchange sites use their own design and are outside the TrustedRouter rollout.

For each group, agents inventory routes and states, choose representative pattern samples for user approval, capture every changed view, run structural/interaction checks, and independently inspect screenshots. Record inspected coverage and unresolved failures; do not equate generated screenshots with review. Authenticated coverage requires real authorized signed-in states. Escalate new design decisions and exceptions to the user, not routine page-by-page QA.


## Documentation coverage — 2026-10-10

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
