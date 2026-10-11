# Shared content layout

Provider routing is the approved reference for checklist items 14 (alignment) and 17 (phone readability). The components are opt-in and loaded by the public, homepage, console, authentication and dashboard shells. Other templates have not yet migrated.

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

Next: migrate docs hub, agent setup and telemetry as a small batch. Their known issues are narrow-screen overflow, fragmented URL columns and split field names. Then cover the remaining docs and other page families using the same foundations, with a route/state inventory.

Agents own automated checks and parallel visual review; the user reviews design direction, not every page. Capture every changed view at appropriate widths, inspect hierarchy, grouping, readable code, spacing and expanded states, and distinguish baseline problems from regressions. Screenshot generation and zero overflow alone do not establish readability. Signed-out console captures do not establish authenticated-console coverage.

## Long-page navigation

Wrap long content in `[data-content-toc]` with a `.content-document` child. Navigation derives its labels and anchors from h2 headings, preserves existing section IDs and generates collision-safe missing IDs. At 1400px and wider it uses a sticky right rail with enough remaining width for examples; smaller screens omit section navigation to preserve reading space. The site header remains in normal flow. Current location is highlighted, navigation focuses the destination heading. Section history returns to section headings; JavaScript-disabled pages retain the readable document without generated navigation.

Reader spacing is deliberately tighter within reference entries and notes. Navigation is not a substitute for reviewing content length and reading effort.

## Compact mobile settings

For Field / Effect / Values tables, `data-reference-accordion` plus a concise `data-purpose` on every row enables native mobile disclosures through 900px. The first entry starts open; readers may open multiple entries. The original desktop table and all technical content remain intact. Enhancement requires a valid three-column table and removes cloned IDs and redundant visual cell labels. Without JavaScript the original table remains available.

Mobile and tablet layouts have no Contents bar or sticky navigation. The desktop sidebar remains visible from 1400px, with full heading labels, original anchors and active-location highlighting.
