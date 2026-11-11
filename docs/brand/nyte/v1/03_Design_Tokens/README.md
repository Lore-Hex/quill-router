# NYTE Brand System V1 · Design tokens

These files go with `NYTE_Brand_Guidelines_V1.pdf` (November 2026). They hold the same system, in a form tools can read.

| File | What it is | For |
| --- | --- | --- |
| `nyte.tokens.json` | Every design token in the W3C Design Tokens Format Module 2025.10: colour with tier, usage and approved text pairs; theme (light / dark); page themes; fonts; typography; spacing; radius; stroke; Band; elevation; motion; layout. 113 tokens; every alias resolves. | Developers, design tools (Tokens Studio, Style Dictionary), AI tools |
| `nyte-tokens.css` | The same tokens as CSS custom properties: palette by tier, semantic light / dark themes, the page lead colour (`data-page="market | price | trust | knowledge"`), type classes, focus ring and reduced motion. | Developers |
| `nyte-components.css` | Component classes (prefix `n-`) that use only the token variables. | Developers |
| `NYTE_Component_Specs.md` | All 23 components: what each one is, what the consumer provides, and the do and don't. | Designers, developers, AI tools |
| `NYTE_AI_Brief.md` | A paste-in brief for any AI model: context, hard constraints (MUST / NEVER) and a pre-flight checklist. | AI tools, reviewers |

Assets are in the NYTE design system artifact, not in this folder: logos, the Verified Secure badge, 43 icons, 10 illustrations, Band patterns and fonts.

The ABC Pelikan fonts (Dinamo) are trial files, for design only. The client licenses the final cuts before production.
