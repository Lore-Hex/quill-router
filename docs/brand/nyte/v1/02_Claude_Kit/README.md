# Claude Kit · NYTE V1

Three ways to make Claude work on brand. Use the skill and the Project together for the best results.

## 1. The nyte-brand skill (recommended)

`nyte-brand-skill.zip` packages the full system so Claude can use it in any conversation:

- `SKILL.md` — when to use it and the step-by-step workflow
- `references/` — AI brief, brand book, component specs, deck templates, approved site content
- `tokens/` — DTCG JSON, CSS variables, component CSS
- `assets/` — logos, badge, icons, illustrations, Band patterns (SVG)
- `fonts/` — ABC Pelikan trial woff2 files

**Install:** in Claude go to *Customize → Skills*, click **+**, choose *Create skill → Upload a skill*, and upload the zip. It then appears in your skills list, where you can toggle it on.

**Requirements:** turn on *Create and edit files* (code execution) in *Settings → Capabilities*. On Enterprise plans an owner enables code execution and Skills in *Organization settings → Plugins & skills*. Uploaded skills are private until you share them with colleagues.

**Use:** just ask — "Make a 3-slide NYTE deck about the Token Index." Claude loads the skill when a request mentions NYTE.

## 2. A Claude Project

1. Create a Project named **NYTE**.
2. Paste `Claude_Project_Instructions.md` into **Project instructions**.
3. Upload every file in `Project_Knowledge/` to **Project knowledge**:
   - `NYTE_AI_Brief.md` — the hard constraints
   - `NYTE_Brand_Book.md` — every rule, with IDs
   - `NYTE_Component_Specs.md` — 23 components
   - `NYTE_Deck_Templates.md` — slide templates T01–T14
   - `NYTE_Site_Content.md` — approved copy, figures and claims
   - `nyte-tokens.css` — tokens as CSS
   - `NYTE_Brand_Guidelines_V1.pdf` — the visual reference
4. Work inside the Project. Attach logo or icon SVGs from this package when you want Claude to place them exactly.

## 3. Any other AI tool

Paste `Project_Knowledge/NYTE_AI_Brief.md` at the start of the conversation and attach `nyte-tokens.css`.

## Tips

- Name the family when you can: Market, Price, Trust or Knowledge. It decides the lead colour.
- Ask for the format you need: a slide deck, an HTML page, a Word document, an image.
- Ask Claude to "run the NYTE pre-flight checklist" before you take the work away.
- Anything Claude puts in [brackets] needs a real, verified value from you.
