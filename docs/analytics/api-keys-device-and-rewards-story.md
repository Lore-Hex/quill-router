# API Keys Device And Social Rewards Analytics

Status: Partially implemented locally. The working checklist is
[Social Rewards Pre-ship Checklist](social-rewards-pre-ship-checklist.md).
Saved October 15, 2026. Production rollout and visual review are pending.

## User Story

As the onboarding owner, I want to know which devices people use on the API Keys
page and where they engage with social rewards, so we can prioritize the layout
and invitation timing without guessing or interrupting activation.

## Existing Evidence And Remaining Verification

We already have acquisition and activation analytics in Axiom. The missing answer
is a reliable mobile-versus-desktop breakdown for the authenticated API Keys page.
Desktop-first usage remains a hypothesis, not an established finding.

On October 15, the inspected `trusted-router-logs` and
`trustedrouter-marketing` field schemas did not expose a usable device category.
A September 15–October 15 query of logs matching `/console/api-keys` in `path`,
`page_path`, or `pathname` returned zero matching records. This does not mean
zero visits.

In `src/trusted_router/middleware.py`, `_is_public_html_response` excludes
`/console` from `public.page_view` tracking. `_user_agent_family` classifies
browsers and bots; Chrome and Safari cannot distinguish desktop from mobile.

The connected Axiom dataset inventory also contains `trusted-router`, an
OpenTelemetry metrics dataset. Its field-schema request returned HTTP 400;
its metric labels remain unverified. Audit and shared demo datasets are not
evidence of customer device usage. Before adding tracking, inspect the metrics
dataset through a supported metrics interface and check any existing web analytics
integration. Reuse a suitable source if one exists. These findings do not establish
that every external analytics service lacks the data.

## Acceptance Criteria

1. Record authenticated API Keys visits with an allowlisted page identifier and
   coarse device category: desktop, mobile, tablet, or unknown. Document
   classification limitations. A narrow viewport alone is not proof of a phone.
2. Use an approved privacy-safe user identifier for unique-user counts. If the
   available identifier represents a workspace/account, label counts accordingly;
   do not call workspace counts people. Do not store raw user agents, IP addresses,
   emails, API keys, prompts, or responses.
3. Provide a reproducible Axiom query/dashboard showing the date window,
   denominator, sample size, unknown category, and data freshness. Distinguish page
   views from unique visitors and show cross-device overlap without double-counting
   it as two separate people. Do not infer historical device percentages from
   browser family.
4. Measure rewards invitation impressions, opening the rewards flow, opening a
   platform link, and successful reward claims. Include placement and platform so
   the early API key invitation, first-successful-request invitation, and persistent
   credits entry can be compared. Keep GitHub identifiable as the first action.
5. Deduplicate retries and define when an impression counts. Use authoritative
   successful credit grants for claims. A link click or self-attestation is not a
   verified follow; report each separately.
6. Exclude preview, internal QA, and known automated traffic from customer results.
   Test duplicates, unknown devices, and telemetry failures. Analytics failures
   must not block key creation, first requests, or reward claims.
7. Track activation and subsequent usage alongside rewards engagement. Comparing
   placements observationally does not establish causation; users reaching their
   first successful request are a selected group. Use a defined experiment before
   claiming one placement improves follows without harming activation.

## Delivery And Scope

Deliver the event contract, regression coverage, reproducible queries, and a
dashboard with explicit coverage limitations. Follow the existing ingestion and
deployment workflow in [Welcome Test And Activation Metrics](onboarding-attempts.md).
Run the repository's required checks before declaring implementation complete;
credit-related changes require the prescribed conformance or differential tests.

Keep this separate from the pending desktop visual review. This story does not
authorize a redesign, a requirement to complete all social actions to enter the
product, or production deployment. Preserve the agreed per-reward sequence:
open that platform's link before claiming its reward.
