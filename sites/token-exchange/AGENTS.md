# Token Exchange design guidance

Applies to this directory. Read README.md for product and content requirements.

## Design direction

- Preserve a minimalist, production-facing landing page: clear hierarchy, restrained typography, one focal visual per section, and deliberate space.
- Treat requests to adjust spacing, color, motion, or imagery as changes to those elements. Do not add unsolicited badges, captions, helper text, footnotes, explanatory labels, extra CTAs, or visible animation/debug controls.
- Prefer subtraction and refinement of existing elements. Add text only when requested or necessary to understand a meaningful action; use the existing FAQ, provider catalog, or onboarding destination for supporting detail.
- Do not reintroduce the removed provider-availability footnote or supplier application instructions beneath the CTA. Preserve accurate relationship labels for provider logos and the substantive privacy distinctions required by README.md.
- Workload priorities describe buyer use cases. “For suppliers” is a distinct section addressing inference providers. Communicate this through the existing headings and layout, not new intermediary labels.
- Keep dark space between colored surfaces, but avoid stacking large spacer bands on existing section padding. Evaluate the total visible gap.
- Let decorative gradients fade into the page. Motion must not expose rectangular layer edges at any point in its cycle. Keep text and buttons stable, and honor reduced-motion preferences without adding visible UI.
- Preview desktop and mobile. For animation changes, inspect multiple phases across the whole cycle, not just whether the animation property is running.
- Keep changes scoped to the exchange site. Shared template/CSS changes affect all exchange markets; they do not require redesigning the main TrustedRouter site.
- Retain the required market directories, factual content rules, and markets.json as the source of per-market copy. Do not fabricate customers, endorsements, statistics, or certifications.
