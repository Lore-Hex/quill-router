# Social Rewards Pre-ship Checklist

Updated October 17, 2026. Production rollout remains on hold.

## October 17 PR checkpoint

User authorized committing and pushing the reviewed console/onboarding work to
the existing draft PR #1790. This supersedes earlier no-push instructions for this
checkpoint only; merging and deployment remain unauthorized.

Reduced the mobile green header to 56px with 4px vertical padding while preserving
the 44px Menu touch target. Desktop sizing is unchanged. Verified the actual 56px
height at 390px and inspected mobile and 1440px desktop screenshots.
All 31 rewards/navigation browser tests passed again; checkpoint Ruff and mypy
passed. Required uv gates still stop at the unsupported Intel macOS torch wheel.

The earlier fallback full-suite run is no longer active. Its final log reports
32 failed, 22,224 passed, 1,337 skipped, 594 deselected, 12 xfailed and 2,254 errors
after 2:29:24. Progress stops around 69% with a truncated traceback and no useful
failure details, so this is neither a clean nor a complete validation result.
Do not attribute these failures to this change without reproduction against an
updated main baseline. No coverage result is established. Keep the PR in draft
pending supported-environment validation and the remaining release blockers.

## Preview UX iteration — direct key reset and console color

User requested a direct dev-only reset and a desktop/mobile hierarchy review.
The local `/tmp/social_rewards_app_preview.py` harness now exposes
`/__preview/key-grant` through a persistent **Fresh API key** toolbar link.
It creates a disposable simulated signup and redirects directly to the one-time
key reveal; repeated use was verified to issue different keys without visiting
the homepage or an OAuth screen. **Key list** opens the current account's keys.
This shortcut exists only in the isolated preview harness, not production routes.

Local presentation changes: sage-green console header, lighter sidebar with a
clearer active item, padded key-management surface, and consistent green rewards
card/dialog header. Copy remains the only filled initial-onboarding button;
Continue retains its strong mint outline. On return visits, the key list and key
creation controls precede optional rewards. Setup stays collapsed and the rewards
headline is preserved. No payout, eligibility, transfer, dismissal, or campaign
configuration changed. YouTube remains unresolved; no replacement or reduction
in the approved reward total has been implemented.

Inspected desktop (1440px), mobile (390px), narrow mobile navigation (320px),
key-list and reward-dialog screenshots. All 31 browser checks passed. Checkpoint
Ruff and mypy passed. The focused server suite returned 114 passed and three
stale presentation assertions: hidden creation disclosure/Help grouping, coding
agent heading, and total-limit copy. Updated those expectations while preserving
key secrecy and budget behavior assertions, and added keys-before-rewards order
coverage; all three focused reruns passed. The pre-existing full backend run is
around 65% and has reported failures; final results and triage remain pending.
No full-suite or release-readiness claim is made. No push or deployment.

## Current validation status — October 16 continuation

This status supersedes older test counts and environment failures below; historical
audit entries remain for context. No push or deployment is authorized.

- Confirmed branch `jasmine/social-rewards` at `f2f04e3bc`, with the approved
  console navigation and 12% mint rewards surface still uncommitted. Unrelated
  untracked assets and handoff folders were left untouched.
- Reran all 29 rewards browser checks and 2 navigation checks: **31 passed**.
  Strengthened the navigation checks for desktop-only single-workspace context,
  mobile Configuration expansion/collapse, separate visible Earnings/Broadcast,
  and restoration across breakpoints; both updated checks passed.
- Confirmed the ngrok simulated signup preview works. Inspected desktop 1280px
  and mobile 390px screenshots, the mobile menu, and the expanded setup tabs.
  No application UI or reward-rule edits were needed in this continuation.
- Checkpoint-environment Ruff passed; mypy passed for all 454 source files.
  Retried each required `uv run` gate using `/tmp/geo-release-tools/bin/uv`:
  all stop before execution because locked `torch==2.12.0` has no Intel macOS
  distribution. Do not change shared dependencies merely to bypass this gate.
- Existing full fallback pytest process (PID 6427) remains active and its log
  `/tmp/console-nav-full-tests.log` continues advancing around 15%. No duplicate
  fallback full suite was started. **No full-suite pass is established.** Its
  invocation is `python -m pytest -q`, without coverage collection; even a pass
  will not establish the required 70% coverage floor.
- A bounded Docker availability check timed out after 15 seconds. Real database
  conformance remains unverified. Proof-oracle completion also remains required.
- Cached `origin/main` is 28 commits ahead of the branch's common base (branch
  has 5 unique commits). Integration with current main and the final release
  review remain pending; no rebase was attempted during the running suite.
- Caller audit: `/shown` is still used to persist automatic-offer suppression;
  its exposure event is distinct from placement observations. `/clicked` has
  no current browser caller, but still has an API regression test. It was not
  removed while the backend suite was executing; PR-scoped cleanup remains open.

Remaining release blockers: supported-environment full gates and >=70% coverage;
real PostgreSQL/Spanner conformance; billing proof oracle; final integrated review;
official YouTube destination and coordinated campaign configuration; internal-QA
reporting exclusions and the approved analytics rollout/verification. Rollout
still sets `TR_SOCIAL_REWARDS_ENABLED=false` and no YouTube destination. The preview
placeholder is not an approved production URL. These checks do not establish
release readiness.

## Next Work In Order

1. Audit reward correctness before refactoring: eligibility, authenticated account,
   original workspace ownership, fixed amounts, duplicate/concurrent claims, and
   failures before or after commit.
2. Add regression tests for uncovered risks and fix confirmed defects. Verify
   recovery after lost responses, failed status reads, and analytics failures.
3. With that protection in place, clean up only code introduced or made obsolete
   by this PR: unused endpoints, obsolete tracking, redundant state, and duplicated
   helpers. Prefer extending established infrastructure. Preserve existing UI.
4. Rerun the required checks and review the final diff for unnecessary scope.

- [x] Finish the initial claim-flow audit and record findings, coverage, and remaining risks below.
- [x] Add and run tests for pre-commit failure, post-commit status failure, pause/resume, authentication, and failed browser reconciliation.
- [x] Confirm the retry test fails when stable claim IDs are replaced with random IDs (process-only mutation; no source changes).
- [ ] Complete PR-scoped dead-code and duplication cleanup after correctness checks.
- [ ] Review the resulting diff and close release-check blockers.

## Analytics

Implementation extends `acquisition.py` and its shared attributed-event logger,
then uses the existing Cloud Logging source filter, growth exporter, sanitized
`trustedrouter-marketing` dataset, and versioned growth dashboard. No additional
analytics service, SDK, datastore, or scheduled worker is introduced. The console
observation endpoint retains authentication, same-origin, and offer-eligibility
checks; it does not broaden the public analytics endpoint.

- [x] Add coarse API Keys device observations without raw user agents or device fingerprinting.
- [x] Record visible rewards invitations, modal opens, and platform clicks with placement.
- [x] Preserve platform names on authoritative reward-claim events during export.
- [x] Deduplicate replayed browser observations and add device/rewards dashboard queries.
- [x] Cover privacy opt-outs, bot/local/test exclusion, validation, and nonblocking failures.
- [ ] Verify any remaining existing metrics source before making an exhaustive claim about missing analytics.
- [ ] Configure exclusion of internal production QA accounts in reporting. Local/test traffic is already suppressed; production employee accounts are not automatically identified.
- [ ] At approved rollout, update the Cloud Logging source filter, deploy the growth worker, publish dashboards, and verify delivery and freshness. Follow [the existing rollout procedure](onboarding-attempts.md).

Device observations count authenticated GET responses to `/console/api-keys`,
not proof of a visible page. They require an existing attribution cookie and honor
DNT/GPC, so they describe observed accounts, not every user. Device classification
is approximate: iPads requesting desktop websites may look like Macs. The dashboard
keeps unknown and multiple-category accounts separate; sum its account groups for
the denominator. It does not reconstruct historical device percentages.

Invitation impressions count the first visible eligible opener for each placement
per page load. Opens and link clicks count interactions. Claims remain emitted only
by the existing committed-credit path; a lost telemetry event is still possible.
Claims have platform information but no claimed originating placement. Do not divide
unmatched claim counts by placement impressions or call clicks verified follows.
Legacy events can have missing placement/platform fields. No credit rules changed.

## Product QA

- [ ] Review the simplified API Keys layout on desktop.
- [ ] Check navigation, scrolling, modal controls, and overflow on mobile.
- [ ] Test fresh signup, key creation, first request, rewards, and return visits.
- [ ] Verify preview reset and homepage navigation.
- [ ] Verify platform URLs, reward amounts, eligibility, expiration, and final copy.
- [ ] Confirm duplicate claims and refresh/retry recovery cannot issue extra credit.

## Checks And Release

- [x] Local browser suite: 21 tests passed, including new analytics coverage.
- [x] Focused Python analytics/export/endpoint tests: 80 passed.
- [x] Whole-repository Ruff and mypy passed in the available fallback environment.
- [ ] Pass all three required `uv run` gates on a supported environment. The pinned
  `torch==2.12.0` has no Intel macOS wheel. The fallback full suite stopped at
  collection with 43 missing-dependency errors, including Pillow and Hypothesis.
- [x] Correct reward-validation tests to expect the application's HTTP 400 response; forged payloads must still leave credits unchanged.
- [ ] Complete PR review, confirm rollout/rollback configuration, and obtain release approval.
- [ ] After approved release, verify product health and real analytics delivery.

No UI changes are included in this analytics increment. Existing layout edits await
visual review. Dashboard queries and event code alone do not mean production
analytics is live.

## Claim Audit Findings

**Ownership race fixed locally; release verification remains pending.** A
reproducing test transfers the original workspace after eligibility succeeds but
before `credit_workspace_once` executes. Before the fix, the request added 30,000
microdollars to the now-transferred workspace. Stable event IDs prevent duplicate
credit but do not enforce ownership at commit time.

- [x] Extend the existing credit transaction with an atomic owner/active-workspace
  condition for this grant, consistently across storage backends. Keep normal
  payment semantics unchanged. Include concurrent transfer/deletion coverage.
- [x] Remove the explicit strict expected-failure marker on
  `test_ownership_transfer_between_eligibility_and_commit_cannot_credit` after the
  fix passes. Both transfer and deletion interleavings now return 403 without
  adding credit. Process-only removal of the atomic guard makes both tests fail.
- [ ] Complete cross-backend conformance on actual configured databases before release.

Existing checks cover fixed server-controlled amounts and original destination,
account isolation, origin enforcement, concurrent duplicates, campaign/plane
eligibility, and lost-response recovery. Additional tests pass for failures before
commit, unavailable receipt reads after commit, paused-campaign recovery, and
unauthenticated/forged identity requests. The browser also recovers when both the
claim response and its immediate reconciliation request fail.

The conformance spending test now uses the existing typed authorize/finalize path
where available instead of the removed Spanner JSON reserve path; its balance and
idempotency assertions are unchanged. Conformance results: 20 passed on memory
and native Spanner fake, 30 skipped for unconfigured database backends. Browser
results: 22 passed. Full `uv run` gates still cannot start on this Intel Mac due to
the pinned PyTorch distribution. These results do not establish release readiness.

Cleanup remains after the ownership fix: inspect the legacy `/clicked` endpoint
now replaced in the browser by `/observe`, distinguish old exposure tracking from
new invitation/open events, and remove obsolete branches only after checking their
callers and tests. Do not change layouts or refactor unrelated money code.

## Abuse Rules Review — Required Before Shipping

Treat verification of the local ownership-race fix as a release gate. It demonstrates a
backend correctness failure, not proof of a practical unlimited-credit exploit.
Do not equate workspace ownership transfers with social-platform ownership.

### Rules Currently Implemented

- Enrollment occurs at qualifying new, direct Google/GitHub signups while the
  campaign is enabled on its configured issuing deployment. Existing accounts
  are not retroactively enrolled. Existing starter-credit exclusions apply:
  Hotmail addresses receive none; GitHub accounts must be at least 30 days old
  with valid creation-time evidence. These are existing rules, not proof of
  comprehensive protection against multiple-account abuse.
- Enrolled accounts receive $0.15 initially and five fixed $0.03 rewards, at most
  $0.15 extra per account for this campaign. Displayed token equivalents are
  estimates; the grant is a fixed credit amount.
- Each claim uses the authenticated user, original enrolled workspace, and a
  server-generated campaign/user/action receipt ID. Clients cannot select an
  amount or destination. Duplicate claims reuse the same receipt.
- The original workspace must currently be accessible and owned by the enrolled
  user. That check is now repeated atomically with credit issuance in the local fix. No permanent
  disqualification after an ownership transfer is currently recorded; returning
  ownership could restore eligibility for remaining actions.
- Claims require same-origin authenticated requests, a recognized action, and
  explicit self-attestation; existing authenticated rate limits apply. The
  browser requires opening that action's link first, but the server does not
  require proof of a link opening or verify an external follow/star/subscription.
- A first API request is not required to claim. Its milestone controls invitation
  placement. Campaign disablement blocks new claims while preserving receipts.

### Proposed Simplifications — Not Yet Implemented

- [x] Enforce current owner, active destination, and once-only receipt inside the
  existing credit transaction. Retain server-fixed amounts and original destination.
- [ ] Audit all workspace ownership-transfer entry points and permissions before
  deciding whether enrolled workspaces should be blocked from transferring or
  permanently lose remaining reward eligibility after a transfer. Do not assume
  a current-owner check implements either policy.
- [ ] Separately audit movement of awarded promotional credits. The app has credit
  transfer routes; determine whether grant credit is already excluded from transfer,
  cash-out, and cross-workspace aggregation by existing provenance rules. Prefer
  extending those rules over introducing a parallel balance or ledger.
- [ ] Review account creation/linking and existing abuse controls: one account is
  not necessarily one person. Rate limits and opening a link cannot prevent a
  determined person from collecting across multiple accounts.
- [ ] Decide explicitly whether low-value self-attested rewards are acceptable.
  More clicks do not verify a follow. If actual verification is required, scope
  supported platform checks separately rather than claiming the current UX proves it.
- [ ] Review an operational campaign budget/pause policy using existing controls.
  Keep any proposed hard issuance cap atomic; a dashboard alarm is not a hard cap.
- [ ] Document the approved rules and test them before removing release blockers.

Recommended baseline: one enrollment, one original destination, five fixed
once-only grants, atomic eligibility checks, and existing credit provenance.
Do not disable all product transfers or add a new fraud subsystem without this
review. Resolve the ownership race first, then remove PR-specific obsolete code.

## Atomic Ownership Fix Validation

The reward claim passes its authenticated user as `required_owner_user_id` to the
existing credit operation. Memory checks under its lock; PostgreSQL locks the
workspace row in the same transaction; native Spanner reads that row inside the
credit transaction so concurrent changes conflict and retry. Missing, deleted,
or differently owned destinations fail before the receipt or balance is written.
The optional condition defaults off for existing payment callers. A grant that
commits before a later transfer remains a valid earlier grant; this change does
not prohibit subsequent transfers or revoke already-issued credits.

- Reward/API and enrollment conformance tests: 66 passed; 42 skipped for unavailable
  real database backends. No expected failures remain in this test selection.
- New backend contract tests cover missing/deleted/transferred destinations,
  absence of a rejected grant receipt, and concurrent once-only conditional grants.
- Both transfer and deletion regression tests fail when the condition is removed.
- Real PostgreSQL/Spanner emulator validation remains pending: Docker daemon is
  unavailable. Required `uv run` gates remain blocked by Intel Mac PyTorch support.

Atomic-fix verification limits: the fallback full suite again stopped at collection
with 43 missing-dependency errors. The fallback four-worker proof-oracle run was
interrupted before completion (four progress dots, no completed result); it is not
a pass and must be rerun in the supported release environment. Whole-repository
Ruff and mypy passed in the available fallback environment.

## Product Rules Discussion — Resume Here

Product review resumed on October 15. Continue one decision at a time; do not
introduce additional unapproved product rules. No push or deployment is authorized.

Explicitly approved in the conversation:

- Preserve existing signup eligibility; split the original allowance into $0.15
  upfront and five separately claimable $0.03 rewards.
- Keep claiming available immediately after signup, before a first API request.
- Keep the marketing list layout; users may claim actions separately in any order.
- Accept user confirmation of the social action without verifying the external follow.
- Preserve the existing "Start with 1 million free tokens" signup headline.
- Rewards are usable on any model; DeepSeek is the token-estimate benchmark,
  not a spending restriction.
- Earned rewards do not expire. Remove billing-period expiry wording from active
  product copy; the current rewards template and script already omit it. Original
  reference images are historical design assets, not approved policy.
- Send rewards to the original signup workspace.

Additional approvals from the same conversation (supersede earlier proposals):

- Remaining claims pause when ownership leaves the enrolled user and resume if
  ownership returns. The new owner does not inherit the claims.
- Preserve existing transfer rules for signup and reward credits; do not add
  special promotional-credit transfer restrictions. The earlier proposed
  non-transferable policy was explicitly superseded.
- Deleting the original workspace does not redirect remaining rewards to a
  replacement workspace or restart enrollment.
- Users may dismiss and return through a persistent entry, with saved progress
  and without repeated automatic popups after dismissal.
- Pausing preserves issued credit and progress while blocking claims. New
  eligible signups receive the original 30 cents while paused and do not become
  eligible for rewards on resume. Previously enrolled users can resume claims.

Desktop copy request: remove the starting-credit dollar display and redundant
one-time-reveal/salted-hash explanations. Retain one practical copy-key reminder
and existing spending/budget information. This changes presentation only, not
credit amounts, eligibility, or storage behavior.

## October 15 Approved-Behavior Implementation Audit

Read-only implementation review; no production requests, push, or deployment.
Only this checklist was updated. Findings concern the current local working tree.

Matching implementation: signup split and existing exclusions; legacy exclusion;
immediate separate claims in any order; explicit self-attestation; stable once-only
grant receipts; unrestricted model spending; existing credit-transfer semantics;
no reward expiry; original destination; current-owner/deletion guards; campaign
pause/resume using the configured 300,000-microdollar fallback signup amount.
The signup headline is unchanged and active rewards copy has no billing-period
expiry wording. Credits has a persistent "Social rewards" button, including when
the campaign is paused. Ownership-return and pause-cohort logic are consistent
with the approved policy, but need explicit lifecycle regression coverage.

Confirmed defect: `static/social-rewards.js` dismisses the dialog and silently
ignores a failed dismissal POST. Automatic opening first persists suppression,
but manual opening before the first API call does not. A temporary browser test
reproduced manual open, dismissal request failure, then a later first-call state
and focus refresh automatically reopening the offer. Fix dismissal recovery and
add a permanent regression test before claiming no repeated popups.

Launch configuration remains intentionally off: rollout sets
`TR_SOCIAL_REWARDS_ENABLED=false` and does not set
`TR_SOCIAL_REWARDS_YOUTUBE_URL`; its default is empty. The campaign cannot enable
without a valid destination. Confirm the official channel and prepare coordinated
release configuration later; do not enable or deploy during this review.

Validation this audit: focused rewards/API and enrollment conformance selection
66 passed, 42 skipped (unconfigured database backends), using the fallback Python
environment and no coverage run. Existing browser suite 22 passed with a temporary
configuration that bypasses the unavailable uv web-server launcher; tests use
mocked responses and real current assets. One additional temporary browser check
successfully reproduced the dismissal defect (it asserts the unwanted reopening,
so its passing result is not proof of correct behavior). Full repository gates,
real-database conformance, and billing proof-oracle remain release requirements;
these focused results do not establish release readiness.

## Dismissal Recovery and Lifecycle Test Follow-up

The confirmed dismissal defect is now fixed locally. Explicit dismissal stores
an account-scoped browser preference, invalidates any in-flight automatic open,
and retries the server dismissal on subsequent refreshes. Manual reopening is
still available. Browser storage failure falls back to suppression for the current
page; persistence across reloads requires browser storage or a successful server
write. No signup, payout, transfer, or eligibility logic changed in this follow-up.

Added browser regressions for failed dismissal across reload/reconnection, blocked
browser storage, and isolation from another account's dismissal. Added backend
lifecycle tests for ownership transferring away and back (including no inherited
claims or repeated payout), and campaign pause/resume with a new 30-cent signup
remaining excluded while prior enrollment resumes with saved progress.

Validation: 25 browser tests passed; 68 focused backend/conformance tests passed,
42 skipped for unavailable database backends. Whole-repository Ruff and mypy
passed in the fallback environment. All three required uv gates were attempted
and blocked by the pinned torch 2.12.0 distribution lacking an Intel macOS wheel.
The fallback full suite stopped during collection with 43 missing-dependency
errors; full-suite success and coverage are not established. No push or deploy.

## API Keys Invitation Preview

Added a compact, eligible-account rewards card above the key list, after any
new-key reveal or empty-state creation form. It opens the existing rewards dialog,
shows claimed progress, and handles paused/completed states without changing
reward policy. Placement telemetry reuses `api_key_setup`.

Real preview exposed an error in the prior dismissal template: shared rendering
exposes `user`, not `ctx`. Corrected the dismissal account attribute to `user.id`
and added a server-rendered regression for API Keys, Credits, and Welcome.

Validation: 29 rewards browser tests passed (including invitation widths 1440,
390, and 320); 12 targeted server-rendered console tests passed. Ruff passed.
Desktop and phone previews inspected, including opening the dialog through ngrok.
The preview is simulated; YouTube remains a preview placeholder.

Compared with current GitHub main API Keys template: key creation, usage/budget
summaries, editing, alerts, strict admission, disable/enable/delete, and new-key
copy reminder remain. Earlier presentation changes removed suggested budget
examples and the `GET /v1/key` explanatory note; these are candidates to restore
inside the spending disclosure, not evidence-backed removals.

A bounded read-only Axiom query on `trustedrouter-marketing` covering the prior
30 days returned zero API Keys/rewards observation events. This does not show
zero console usage or prove the redesign is better. The supplied top-pages image
lists public landing pages and gives no API Keys control-level usage evidence.

### Welcome screen visual simplification

Restored the `GET /v1/key` remaining-budget tip inside new-key Spending limits and existing-key Edit limits. Removed duplicate decorative 02/03 labels, redundant eyebrows and Ready to run text from onboarding. Shortened the first-request explanation and removed outer borders from the test and connection sections. The early rewards entry is now a mint button, still available before first call under the same eligibility rules. No reward API or claiming behavior changed. Desktop and 390px preview inspected; no mobile horizontal overflow. Rewards browser suite: 29 passed. Full backend validation remains separate and incomplete.

### Onboarding attention hierarchy

Keep saving the one-time key and running the first request primary. Coding-agent instructions now live in a closed-by-default “Use with a coding agent” disclosure below the visible key/copy control. The early rewards button is an outlined secondary action next to the first-request button (wrapping below on phones), instead of preceding key setup. Removed arrows from first-request and early/API Keys reward CTAs. Immediate claiming, eligibility, dismissal, and post-success invitation rules are unchanged. Verified disclosure expansion/collapse and 390px layout in the preview.

### Post-request success cleanup

Success now replaces first-call instructions/actions, with added spacing before confirmation. Fixed the early rewards button's hidden-state CSS so it cannot remain alongside the post-success invitation. Moved PONG into Request details; invitation explains social rewards without prescribing GitHub first. Removed both decorative key containers on welcome while retaining the key field, Copy, workspace destination, one-time warning, and agent disclosure. Browser suite: 29 passed including early-entry disappearance after success; actual preview verified at desktop and 390px. No reward backend behavior changes.

### Collapsed app setup

Welcome setup examples now sit in a closed-by-default Connect your app disclosure, labeled Python, JavaScript or curl. Existing Connect your app anchor opens it. Removed redundant workspace caption beneath the one-time key and the first-request explanatory sentence. Default first-request CTA is Test my key, with Testing your key and Try again states. Reward rules unchanged. Actual preview verified for successful request and setup expansion/collapse at 390px without overflow.

### Approved key-first onboarding (supersedes first-call UI above)

User approved removing the welcome test step and progress tracker. Welcome now leads with Your API key and Copy, retains one-time reveal semantics, keeps coding-agent and SDK setup collapsed, and presents a secondary rewards invitation with existing progress/pause/eligibility handling. No request is required to open rewards. Removed exposure logging for the no-longer-rendered first-call button experiment. Ledger, eligibility, claim, dismissal and backend first-call recording rules unchanged. Phone preview verified at 390px; immediate rewards opening checked. Updated welcome regression expectations for the approved flow.

### Invitation typography cleanup

Removed the invitation eyebrow and separate claimed-count label; detailed progress remains in the dialog and remaining rewards still appear in the invitation sentence after a claim. Both optional setup disclosures use matching 14px regular type, without the extra SDK subtitle. Budget recommendation (not implemented): an optional Spending limits disclosure for the existing signup key, preserving current defaults and enforcement. Main snapshot has create/edit budget controls but none on welcome.

### Layout-only polish

Welcome content constrained to 720px; reduced one-time-save instruction emphasis; adjacent setup disclosure rows use consistent padding without the previous grid gap. Rewards CTA now sits below its copy; Continue is separately left-aligned below. Copy retains the sole filled-button treatment on the initial screen. No text, labels, or behavior changes. Desktop and 390px preview inspected; no horizontal overflow.

### Visual QA: prioritize navigation over setup

Combined coding-agent and SDK setup into one collapsed Connect your app disclosure. Coding agent is the first tab; Python, JavaScript and curl remain available through the existing tab mechanism. Removed the competing outlined setup controls; Continue stays outlined and Copy stays filled. Rewards box preserved. Inspected actual screenshots for collapsed/expanded desktop and 390px phone states, switched to Python, and corrected coding-agent message overflow with wrapping. No reward/backend behavior changes.

### Compact mobile console navigation

At widths up to 800px, console header shows the brand and Menu. Existing workspace selector, navigation, resource links and account controls move into a native modal drawer; desktop restores the same nodes to their original positions. No duplicate form state or navigation destinations. Escape/Close dismisses, navigation links close the drawer, focus returns to Menu, and background scrolling is locked. Without JavaScript, the prior navigation remains available. Desktop sidebar and rewards box styling unchanged. Visual QA covered 390px closed/open drawer and 1440px restored desktop; corrected inherited horizontal sidebar styles found during inspection. Added 320px/390px drawer regression checks for focus containment, dismissal, destinations and resize restoration.

### Mobile navigation decision superseded

User rejected the compact header/drawer because it deviates too far from live main. Reverted that entire shell change and its dedicated tests, restoring the preceding header/sidebar behavior. Key-first onboarding, setup grouping, and rewards presentation remain intact.

### 2026-10-16 scope decision: ship rewards before shared-header work

User explicitly deferred header centralization and navigation redesign to a separate chat and isolated worktree. This is follow-up work, not a prerequisite for this PR. No follow-up chat or worktree has been created by this decision record.

Current PR scope: preserve existing console header, desktop sidebar, mobile navigation, workspace switching, approved key-first onboarding and reward behavior. Fix the console/provider brand link to go to `/` rather than returning to API Keys or provider overview. Do not transplant the reviewed navigation mockups into this PR. No push or deployment is authorized yet.

Follow-up scope: shared brand/header primitives with public, console and minimal-auth variants; one console navigation definition used by desktop and mobile; clear account actions and workspace context; actual brand-kit typography, assets, spacing and interaction states. Review mobile single-workspace and multi-workspace states before implementation. Earnings and Broadcast must not be grouped under Configuration merely to shorten the menu. API reference and independent trust verifier are later exceptions; separately branded Token Exchange sites are out of scope.

Parallel-work strategy: start the follow-up worktree from then-current main, keep header work on its own branch/PR, and avoid changing reward or key-flow templates. Design exploration can proceed while this PR finishes validation. After this PR merges, update the header branch onto main and validate it against the shipped onboarding/rewards UI; do not make this PR depend on the header branch. Shared files such as console/_layout.html, console.css and console.js are expected integration points, so avoid incidental refactors here.

Audit evidence: public pages, docs and homepage already share public/_nav.html; console and provider share console/_layout.html; auth has a minimal brand shell. Axiom's checked 30-day window did not provide console device-category or workspace-switch usage evidence, so mobile use must not be assumed negligible. The current branch adds API Keys device-category instrumentation; evaluate it after release before prioritizing navigation by usage.

### 2026-10-16 approved console-only navigation pass

Supersedes the instruction to freeze console navigation above. User approved a bounded console-only change: mobile brand plus Menu; workspace selection inside navigation; desktop workspace selector in sidebar with Models, Docs, Help and Account in the header. One workspace displays its name without a redundant selector. Configuration is collapsible on mobile; Earnings and Broadcast stay separate. Existing workspace POST and return path are preserved, with one sign-out form moved between mobile navigation and desktop Account. Logo links home. API key and rewards content/behavior unchanged; cross-site header centralization remains deferred. Desktop 1280px and phone 390px screenshots inspected; navigation tests cover one/two workspaces, POST submission, Escape, resize and 320px overflow. Ruff and mypy passed in the checkpoint environment; full pytest is still running in /tmp/console-nav-full-tests.log. No push or deploy.

### Brighter key-page hierarchy

User approved stronger Continue emphasis over optional rewards, while Copy remains the sole filled onboarding button. Continue now has a stronger mint outline and weight; rewards use a quieter panel and reduced padding. Key pages use the brand's bg-alt canvas, brighter secondary text and inset key surface. Single-workspace names are hidden in navigation; multiple-workspace switching remains. Desktop and mobile screenshots inspected, including the single-workspace menu. Reward rules and API key behavior unchanged. No push or deploy.

### Desktop workspace context and rewards surface restored

User requested the single-workspace name back on desktop only; mobile still omits it. Restored the brighter selected-green rewards surface so the card's internal padding reads as a deliberate inset rather than floating text. Continue retains its stronger outline and weight; reward CTA remains secondary. Desktop and mobile preview inspected. Reward behavior unchanged.

### Handoff checkpoint: brighter rewards card

Latest user adjustment: increase the welcome rewards surface brightness by mixing 12% brand mint into selected green, retaining the stronger Continue outline and Copy as the only filled CTA. Visually checked the updated preview. Current local commits: f2f04e3bc (key-first UI) and 961949d98 (reward behavior checkpoint); subsequent console-only navigation, contrast and workspace-label changes remain uncommitted. New tests/browser/console-navigation.spec.js is also uncommitted. Do not add unrelated untracked brand assets or handoff folders to commits. No push or deploy authorized. Full backend pytest log remains /tmp/console-nav-full-tests.log (last observed around 15%; no final result yet). Browser navigation tests (2) and rewards tests (29) passed, as did Ruff and mypy in the checkpoint virtualenv; these do not replace full release validation.

Preview: https://fd77-108-6-58-177.ngrok-free.app/auth/google/login starts a fresh simulated signup and reveals a disposable key. Server uses /tmp/social_rewards_app_preview.py on port 18085, isolated in-memory state. Refreshing /console/welcome consumes the one-time key; use the signup preview URL again. Backend campaign rollout and actual YouTube destination remain release checklist items. Shared-header centralization remains separate future work; the user subsequently approved the bounded console-only navigation implemented here.
