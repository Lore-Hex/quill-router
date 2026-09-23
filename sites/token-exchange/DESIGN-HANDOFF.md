# NYTE design handoff

Updated September 23, 2026. This is a working history, not approval of every
current visual choice. The user wants a fresh chat because recent iterations
feel less polished. Start by looking at the page, not by adding more CSS.

## Repository state

- Repository: `quill-router`; site: `sites/token-exchange/`.
- Branch at handoff: `jasmine/nyte_initial_polish`.
- Baseline before this checkpoint: `18721c91` — Center desktop market navigation.
- This handoff ships with the mobile/motion refinement checkpoint; use `git log`
  for its commit hash and `git status` for any subsequent work.
- Earlier milestones: `46126c56` (mobile layout/unified market navigation),
  `69697556` (landing-page redesign).
- This checkpoint includes refinements in `exchange.css`, `exchange.js`,
  `template.html`, and `verify.cjs`, plus this handoff and its AGENTS.md link.
- The user explicitly requested committing and pushing the branch after the
  handoff was written. GitHub accepted the push dry run on September 23; earlier
  permission-denied reports are historical. Verify the remote commit before
  beginning cloud work. Production deployment was not requested.

## Product and source of truth

`build.py` renders `template.html`, `exchange.css`, `exchange.js`, and
`markets.json` into static pages for 12 markets / 29 domains. Per-market copy
belongs in `markets.json`. Shared design changes affect all exchange markets,
not the main TrustedRouter website. Read README.md and AGENTS.md here.

Plain-language product explanation: a business wants an AI model to do work,
such as summarizing documents. An inference provider operates the computers
that run the model. The buyer pays for that processing, commonly measured in
input/output tokens (pieces of text). TrustedRouter provides the routing layer;
the exchange page invites buyers to evaluate providers and suppliers to offer
inference capacity. This is not a cryptocurrency or securities exchange.

The buyer diagram shows **Your workload → TrustedRouter → eligible providers**.
It illustrates routing/choice; it is not live traffic, a broadcast to every
provider, or a promise that every provider is eligible. Keep this orientation
consistent. Explain the product before changing its diagram if the user asks.
“For buyers” and “For suppliers” are headings, not four buyers/four suppliers.

## Design direction that should survive a new chat

- Tiun is inspiration for hierarchy, rhythm, subtle grid, and restrained motion,
  not a template to copy. References: https://tiun.io/ and
  https://kage.design/designs/tiun-landing-page .
- Minimalism means fewer competing elements, not removing all color or making
  large empty stretches. One focal visual per section; intentional spacing.
- Centered hero; mixed centered/left-aligned sections on a coherent grid.
- Preserve the value-proposition headline, “Intelligence, bought on your terms.”
  The exchange name already appears in the header/footer.
- Retain current serif heading / sans body direction unless explicitly changing
  typography. Do not redesign the whole monorepo for brand consistency.
- Readable body text, slightly quieter than headings. Current body/muted color
  is `#d9ddda`; headings are `#f3f3ef`. Avoid turning body copy faint again.
- Preserve visible teal/mint and blue color: a strong hero arc and closing CTA,
  softer supporting artwork. Avoid generic gradient-filled boxes everywhere.
- Secondary CTAs are styled buttons. Do not return them to overlooked text links.
- No unsolicited captions, micro-labels, badges, disclaimers, helper copy, extra
  CTAs, or animation/debug controls. User repeatedly objected to these.
- Do not restore the removed provider-availability footnote or supplier application
  instructions below the supplier CTA. Supporting detail belongs at the destination.
- No invented stats, customers, endorsements, certifications, or local-processing
  guarantees. Provider ZDR policy and verified confidential inference are distinct.

## Layout decisions and iteration history

1. Hero was simplified, with value proposition promoted; user later explicitly
   preferred centering inspired by Tiun. Earlier left-align instructions are stale.
2. The two geographic categories were combined with explicit user approval.
   Keep ONE directory in header and footer, same ordering. Desktop header centers
   when links fit; narrow layouts scroll horizontally with directional controls.
   README now reflects this. Do not restore two rows based on old conversation.
3. Three workload cards remain: investment research, legal operations, financial
   platforms (New York copy). They describe buyer use cases. Suppliers are a
   distinct audience/section and need visible breathing room after these cards.
4. Excessive spacer bands were reduced. Do not stack empty bands plus large
   section padding. Inspect the total gap; colored sections should not collide.
5. Provider strip now has six catalog examples: OpenAI, Anthropic, Mistral,
   Google Vertex AI, DeepSeek, xAI. Assets come from the monorepo. The label is
   “Providers on TrustedRouter.” These are not customer logos or endorsements.
   Desktop has a row; narrower layouts use a grid. Preserve clear section borders.
6. Closing CTA “Bring your demand. Bring your capacity.” remains after the FAQ.
   Its colored background moves slowly with texture, without exposed layer edges.
7. Footer was compacted on mobile; geo list scrolls. Do not re-expand into a tall
   wrapped list or add explanatory scroll text.

## Motion/mobile refinements in this checkpoint

- Hero arc pulses on a 10-second loop rather than fading in once. Mobile minimum
  opacity is .82 (desktop .66), with a brighter mobile gradient.
- Desktop buyer graphic retains a circular router; mobile uses a compact
  rectangular router and a small workload pill. Keep all content stationary.
- Buyer animation is an 8-second loop: workload connector, router highlight,
  provider highlight. Independent animated SVG strokes were replaced by a static
  base and fixed overlay whose opacity changes, following reported horizontal
  glitches. Chrome geometry checks were stable; that does not prove every phone
  rendering issue is fixed.
- Supplier paths loop slowly (9 seconds), over a static illustration.
- Loops pause offscreen/in a hidden tab and honor reduced motion. No visible
  pause/debug control was requested.
- Mobile hero used to stretch and move CTAs as the browser address bar collapsed.
  JS now freezes initial computed hero top padding/CTA gap and the short-screen
  art treatment. Recompute on WIDTH changes/rotation, not height-only changes.
  Preserve `hero-layout-locked`, `hero-compact`, and associated CSS custom props.
  A regression check in `verify.cjs` tests height changes and rotation.

## Latest changes: implemented, awaiting the user's visual judgment

- Mobile Back to top moved into the footer's upper-right corner beside the brand,
  with a 44px target, thin neutral arrow, subtle circular outline, and accessible
  name. It is separate from the geo chevron. Desktop hides it.
- Mobile “Your workload” pill was first too bright, then too dark. Latest:
  background `#30483e`, text `#e8efeb`, thin sage border, 14px normal-weight text.
  This is a candidate balance, not settled user approval.
- User saw a blue line at the mobile header. Chrome did NOT reproduce that blue
  line. `.masthead:focus { outline:none }` suppresses a suspected native anchor
  focus artifact; interactive links/buttons keep their focus rings. Do not claim
  the cause is proven or dismiss a continued report.
- Main next step: inspect the whole mobile composition and discuss what feels
  unpolished. Avoid another isolated patch that disrupts nearby hierarchy.

## Preview and validation

Local preview: http://localhost:3000/new-york/ (BrowserSync).
QA server: http://localhost:8089/new-york/ . Output: `/tmp/te-build`.
Processes may not survive a new session; check before assuming either is running.
BrowserSync ghostMode is disabled so phone and desktop do not scroll together.
Ngrok previously forwarded port 3000; the URL needs `/new-york/`, not just `/`.
Ngrok/local preview requires the laptop to stay awake and connected.

```sh
python3 sites/token-exchange/build.py --output /tmp/te-build
python3 -m unittest discover -s sites/token-exchange -p 'test_*.py' -v
# Only if no QA server is running:
python3 -m http.server 8089 --bind 127.0.0.1 --directory /tmp/te-build
# With Playwright installed:
node sites/token-exchange/verify.cjs /tmp/te-build
```

Checkpoint validation: build passed, 9 unit tests passed, JavaScript syntax
checks passed, and full browser checks passed 12 markets × 3 widths, including
the mobile hero resize regression and no-JS navigation. Targeted Chrome checks
also passed at 320, 390, and 1440px for footer layout, Back to top navigation/
keyboard focus, and horizontal overflow. Screenshots were visually inspected. These are browser viewport checks, not a real iPhone test.
Root CI gates (`uv run ruff check .`, `uv run mypy`, `uv run pytest -q`) remain
blocked locally because `uv` is unavailable. Never describe them as passed.

Current machine has Playwright under `/tmp/te-preview-tools/node_modules` and
Chrome at `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`.
The in-app browser runtime was unavailable in recent turns; disclose fallback
and follow the browser skill when available. Do not assume these machine paths
exist in Codex Cloud. Animation QA needs several phases/full cycles, not one PNG.

## Hosted preview and work away from the laptop

Existing public design preview:
https://nyte-design-review-jasmine.jasmine383941.chatgpt.site

**This is an older snapshot of `18721c91`, not the newer mobile/motion checkpoint.**
It was published separately from NYTE production and is not automatically synced.
Its local project is `/Users/jasminelee/Development/nyte-design-preview` with
generated files in `dist/` and a `.openai/hosting.json` linkage. Reuse that project
if updating; don't create duplicate sites. The root is the New York page; local
market links and noindex were set for the design preview. The monorepo remains
the source of truth; edits to generated hosted files must be ported back.

Options researched in official docs on September 23, 2026:
- Codex Cloud checks out a GitHub/GitLab branch and works independently of the
  laptop. Select the branch containing this checkpoint and handoff. A cloud task is not itself a promise of a persistent public preview.
- A hosted preview is the second part of the workflow: update it after each
  reviewed iteration, then inspect its URL on the phone. Sites is already in use
  here. Direct Sites editing is another option, but can diverge from this generator.
- ChatGPT mobile Remote can steer the existing desktop environment, but its
  host must remain awake/online. It does not solve a sleeping-laptop requirement.
- Exact account access and repository permissions still need checking during setup.

Sources: https://learn.chatgpt.com/docs/cloud ,
https://learn.chatgpt.com/docs/environments/cloud-environment ,
https://learn.chatgpt.com/docs/remote-connections ,
https://learn.chatgpt.com/docs/sites .

## Fresh-chat prompt

Continue the New York Token Exchange UI in `sites/token-exchange/`. Read
`AGENTS.md`, `README.md`, and `DESIGN-HANDOFF.md` in that directory, then inspect
the branch and working diff. Preserve uncommitted work. The handoff records
settled choices and unresolved issues; do not assume the latest visuals are
approved. Open the current page at mobile and desktop widths before editing.

My requested iteration: [describe one concrete issue and attach a screenshot].

Explain your diagnosis briefly, then implement the smallest coherent refinement.
Preserve the minimalist hierarchy, readable text, intentional color, stable
animations, and existing copy. Do not add helper text, badges, controls, sections,
or unrelated redesigns. Judge spacing across neighboring sections. Inspect the
result at 320/390px and desktop, including scrolling and a full animation cycle
when affected. Tell me what changed and what you actually verified. Update the
handoff with new decisions. Do not commit, push, or deploy unless I ask.
