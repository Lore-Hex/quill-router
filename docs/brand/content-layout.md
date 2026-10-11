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
