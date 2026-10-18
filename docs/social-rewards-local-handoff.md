> Historical handoff. For the current reduced release, see
> [the release checklist](analytics/social-rewards-pre-ship-checklist.md).

# Social rewards: local implementation handoff

October 15, 2026. Branch `jasmine/social-rewards`, based on `4f6a87efe`.
Local implementation only; no PR, push, deployment, live signup changes or real rewards.
The current implementation request supersedes the plan's older production-delivery text.

## Preview and behavior

Open `docs/previews/social-rewards.html`. This is a self-contained simulation using the
actual rewards template, CSS and JavaScript, with 1440/390/320 width controls and
available, partially claimed, pending, error, before-call, key-reveal, paused and
ineligible states. It performs no real requests or grants. Regenerate it with
`python3 scripts/build_social_rewards_preview.py` after UI changes.

All five actions use self-attestation. New eligible direct Google/GitHub accounts
receive 150,000 microdollars; each of five claims issues 30,000 through the existing
typed grant primitive. Account/action event IDs and durable enrollment fix the
destination to the initial owned workspace. Spending or switching workspaces does
not reset progress. Existing young-GitHub, email, wallet and delegated signup
exclusions remain in force. Legacy accounts are not enrolled or reduced.

Enrollment and the signup grant share the backend transaction. Committed grant
receipts determine claimed state; uncertain responses retry the same event ID.
POSTs require authenticated console context, existing rate limits and same-origin
requests. Amounts, eligibility and workspace IDs cannot be supplied by the client.
The first-call marker is independent of optional marketing attribution and uses
the existing successful-call callback; bounded existing activity provides recovery.
No reserve/refund or money settlement primitive has been changed.

Recovery limitation: if the successful-call marker write fails and the only
success ages beyond the latest 100 activity events, another successful call may
be needed. Ordinary transferred/deleted destinations are rejected; ownership
validation and credit issuance are separate transactions, so concurrent
administrative ownership changes are not atomically serialized with a claim.

The popup reserves its once-only display on the server, defers during key reveal,
and can be reopened from Credits. Billing remains dollar-based. No expiry, social
verification, account linking, farming detection or campaign-wide cap was added.

## Configuration and launch prerequisites

`TR_SOCIAL_REWARDS_ENABLED=false` is the off switch. Defaults and deployment scripts
leave it false. Enrollment and claims also require a nonempty local plane ID equal
to `TR_SOCIAL_REWARDS_ISSUER_ID` (default `gcp-primary`) and a confirmed HTTPS YouTube
channel URL in `TR_SOCIAL_REWARDS_YOUTUBE_URL`. Missing configuration preserves
normal signup grants. Turning the switch off stops new enrollment/claims while
preserving earned credit and saved progress. Peers are explicitly disabled.

Before any launch:

- Resolve platform policies for incentivized engagement.
- Obtain explicit incident/off-switch ownership. Jasmine has not accepted on-call
  or ongoing monitoring responsibility.
- Confirm the official YouTube channel; it was not established by available
  repository sources or public retrieval. No guessed channel ships as a default.
- Pass the full Python, coverage, backend conformance and browser gates below,
  review actual desktop/mobile rendering, and use normal reviewed release gates.
- Confirm exporter deployment and event delivery (checklist item 15 remains open).
  Exposure/click telemetry is not follower verification. Grant telemetry is
  best-effort; committed typed receipts remain the accounting authority.
- Confirm the single issuing deployment. Independent databases cannot deduplicate
  rewards if operators deliberately configure multiple deployments as the issuer.

The published DeepSeek V4 Flash 0731 DeepInfra route was rechecked at
https://trustedrouter.com/models/deepseek/deepseek-v4-flash-0731 : customer pricing
was 0.0633 input / 0.1899 output per million tokens. Equal input/output costs
0.1266 per million total tokens, supporting conservative approximately 200K/action
and approximately 1M/campaign copy. Recheck before launch; amounts never follow
automatic price refreshes. Existing homepage 1M signup copy remains numerically
supported by the same benchmark, but is not a guaranteed token balance.

## Actual local validation

- Full Ruff check passed using cached Ruff 0.17.0. Exact `uv run` commands could not
  start because `uv` is missing from PATH.
- Cached Mypy checked 454 files and reported three errors outside the modified
  files: `public_openapi.py:72` unused ignore, `serve.py:86` undetermined
  `should_exit` type, `async_settle_shadow_v2_certificate.py:62` unused ignore.
  This is not a passing Mypy gate; missing dependencies may affect diagnostics.
- Full pytest, coverage and focused money/conformance attempts cannot collect:
  FastAPI is missing. Dependency installation fails on network/DNS access.
  Coverage >=70% is not established. Docker is not running, so real PostgreSQL/
  Spanner emulator conformance is not established either.
- CSS lint, frontend TypeScript check, frontend ESLint, JavaScript syntax,
  Python syntax and diff whitespace checks passed.
- Three direct export projection checks passed, preserving reward events while
  keeping grants distinct from purchased-credit revenue.
- Thirteen browser tests are defined. Eleven were attempted and failed before
  test bodies because Chromium launch was denied by the OS sandbox
  (`MachPortRendezvousServer`, permission denied 1100); the two subsequently added
  regression tests have only been syntax-checked. Browser security policy also
  rejected the file preview URL. No rendered screenshots or visual approval.

Tests added cover concurrent/repeated claims, lost responses after commits,
fixed destination and total, first-call privacy, cohort/exclusion rules, forged
claims, CSRF, disabled/peer planes, persistent display suppression, analytics
failures and backend enrollment/grant conformance. Their presence is not a claim
that execution passed. Multiple-account farming remains unsolved.

## Changed areas

- `social_rewards.py`, console rewards routes, OAuth cohort selection and config.
- Storage models/protocol plus memory, Spanner and PostgreSQL enrollment/receipt
  methods; Spanner fake and SQL conformance manifest.
- Existing acquisition callback and gateway callback arguments; analytics exporter
  allowlist and tests. PostgreSQL's pre-existing unsupported `signup` remains so.
- Console dialog template, layout include, Credits entry, CSS and JavaScript.
- Python and browser regression tests, preview generator and generated preview.
- GCP/AWS/Azure config-as-code defaults; all remain disabled.

## Local continuation validation (October 14 session)

This session first tested local `.git` writes with a temporary-file probe: the OS
returned `Operation not permitted`. The branch remains `jasmine/social-rewards`;
no staging, commits, rebase, push, PR, merge, deployment or real rewards occurred.
Existing uncommitted work was retained.

Fixed the automatic-popup race by rechecking key reveal and other open dialogs
after the server display reservation returns. If either appears during that
request, the reserved automatic offer stays suppressed; Credits can still reopen
it. Added keyboard focus wrapping in both directions and mobile header clearance
so the close button does not crowd the title at 320px. Regenerated the preview.

The earlier browser-launch limitation was overcome through approved sandbox
escalation. The isolated mocked browser suite now passes **15/15** using Chrome,
including two delayed-response popup regressions, keyboard wrapping, and header
geometry at mobile widths. Fixed the test fixture's missing UTF-8 declaration,
which had corrupted punctuation and caused claim-button selectors to time out.
Desktop and mobile screenshots at 1440/390/320px were generated and visually
reviewed (`/tmp/social-rewards-{width}.png`). These are mocked UI checks, not
backend or production verification; they supersede the earlier browser result.

Current remaining validation limits:

- All three exact `uv run` gates were attempted but cannot start: `uv` is absent.
  Installing it into a temporary directory failed with no available distribution.
- Cached Ruff 0.17.0 passes the full tree. Cached Mypy still reports the same three
  errors listed above (454 files checked); the Mypy gate is not green.
- `python3 -m pytest -q` still fails loading conftest because FastAPI is missing.
  Full Python tests and >=70% coverage remain unverified.
- Docker cannot connect to its daemon; real backend conformance remains unverified.
- Frontend TypeScript, ESLint, CSS lint, JavaScript syntax and diff whitespace
  checks pass. No backend or credit logic changed during this continuation.

## Local checkpoint validation (October 14, 2026)

Reviewed the social-rewards source, storage changes, disabled deployment defaults,
tests, preview builder and generated preview for checkpoint scope. Unrelated
homepage handoff directories, images and brand-token files are excluded. This is
a local checkpoint only, not a release-ready change; no push or deployment is
authorized. Git staging and commit use the supported sandbox escalation flow.

- Installed `uv` 0.13.1 in `/tmp/social-rewards-checkpoint-tools`. All three
  required commands (`uv run ruff check .`, `uv run mypy`, `uv run pytest -q`)
  were attempted against the project and stopped during dependency setup:
  locked `torch==2.12.0` has no Intel macOS wheel or source distribution.
- A temporary-environment retry excluding optional `torch`/`torchvision` also
  failed: locked `cbor2==6.1.4` requires Rust edition 2024, unsupported by the
  installed Cargo 1.84.0. Repository dependency declarations and lockfile were
  not changed to work around these platform limits.
- Standalone Ruff 0.17.0 passed the full tree. Standalone Mypy 2.1.0 checked
  454 source files and reported the same three diagnostics listed above, all
  outside the checkpoint changes. The environment lacks the full dependencies;
  this is not a passing type-check gate or proof of the diagnostics' cause.
- Full pytest via the system Python also failed at collection because FastAPI
  is missing. Explicit conformance, proof-oracle and coverage invocations via
  `uv run` hit the same PyTorch setup blocker. Python behavior and coverage
  >=70% remain unverified. Docker was checked with escalation and its daemon
  is unavailable, so real database conformance remains unverified.
- The mocked social-rewards browser suite passed **15/15** in local Chrome.
  Frontend TypeScript, ESLint, CSS lint, JavaScript syntax, new Python file
  syntax and diff whitespace checks passed.

No rebase was attempted with this uncommitted work. Dependency setup failures
are environment blockers, not attributed to a source regression. Run all release
gates in a supported environment and reconcile with current main before release.

## UX revision (October 14, 2026)

Removed user-facing "Optional" and honor-system copy at Jasmine's request.
Each reward now starts with an outbound action button; opening its destination
unlocks that action's claim button. Jasmine chose this per-reward UX over gating
access to the whole product. Opening a link alone never issues a grant. This is
a browser interaction gate, not proof of a follow; opened-link state resets on
page reload while committed claims remain durable. Updated browser suite: 16/16
passed, including per-action unlock, no grant on link open, retries and layouts.
Required Python gates were retried and still stop at the locked PyTorch platform
blocker. The local preview uses temporary runtime dependencies, not the CI lock.

Placement review: the supplied mockup and feature plan point to the successful
first-call screen. That timing mismatch is addressed by the October 15 invitation revision below. No separate
marketing brief authorizing an all-actions product-access gate was found.

Local preview reset repair: the temporary `/tmp/social_rewards_app_preview.py`
harness now uses `clear_session_cookie` to clear both session and sign-in hint.
Previously Start over left `tr_signed_in=1`, making homepage links redirect to
Console and back without opening sign-in. HTML responses now disable caching and
clear stale hints when the in-memory session no longer exists after a restart.
Verified in the actual app: homepage signup opens the dialog, simulated Google
signup reaches onboarding, first call succeeds, rewards opens after API keys,
signed-in Console works, and Start over restores working signup controls.
These changes repair the temporary preview harness, not production auth behavior.


## Success-screen invitation (October 15, 2026)

The first-call success screen now offers **Unlock extra tokens** beside
**Continue to console**. It appears only when the successful result is visible
and the server confirms eligibility, an enabled campaign and first-call evidence.
The browser success event triggers a server reread; it cannot authorize a reward.
The modal opens only after the invitation is clicked, with GitHub first.
Displaying the invitation consumes the automatic offer so continuing to API keys
does not immediately open another popup. Credits remains the return path;
accounts whose first call happens externally retain the next-console offer.
The key reveal is never automatically covered. Failed or ineligible first calls
do not show the invitation, and standard setup actions remain available.

Browser checks cover the invitation at 1440, 390 and 320px, server evidence,
explicit opening, GitHub ordering and focus restoration. Full Python gates were
retried with supported escalation and remain blocked by torch==2.12.0 lacking an
Intel macOS wheel/source distribution. No push or deployment performed.

## Visual simplification and deliberate claim step (October 15, 2026)

Removed nested success/reward cards and the completed run button. Request
metadata now lives under Request details. The reward invitation is an unboxed
section with a short headline and two actions. Rewards uses monochrome platform
marks, divider-separated rows and short Star/Follow/Subscribe buttons; removed
all outbound arrow glyphs. Workspace and conversion details remain available
under About these rewards.

Opening a destination expands only that action's instructions and an unchecked,
action-specific completion confirmation. Claim remains disabled until confirmed;
unchecking locks it again. This browser gate does not verify platform follows.
GitHub remains first. No OAuth scopes, external follows or production billing
were changed. Real verification would be separate platform-integration work.

All 19 browser tests passed; a focused follow-up also checks confirmation toggle
behavior. TypeScript, CSS lint, JS syntax and diff whitespace checks passed.
All three required Python gates still fail during dependency setup because
locked torch==2.12.0 has no Intel macOS distribution. Actual local app inspected
at desktop and 390px widths; local server uses the existing simulated harness.
