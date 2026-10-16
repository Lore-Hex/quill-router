# Social Rewards Pre-ship Checklist

Updated October 15, 2026. Changes are local; production rollout remains on hold.

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
