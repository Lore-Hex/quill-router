# Social rewards reduced release

Updated October 17, 2026. This replaces the historical design and review log;
previous revisions remain in Git. Keep one PR, freeze feature scope, and fix only
release blockers. No production changes or push are implied by this document.

## Release behavior

- Primary GCP deployment enables rewards in code. The single-issuer restriction
  remains; AWS/Azure must not create a second independent reward allowance.
- Eligible new signups receive 150,000 microdollars. Four available actions each
  grant 30,000, giving 270,000 now. A valid official YouTube channel later restores
  the fifth action and the existing 300,000 lifetime maximum for this campaign.
- Missing/invalid YouTube configuration hides only that action and rejects direct
  claims for it. It does not disable enrollment or the other four actions.
- Claims work immediately after signup, are idempotent, and stay tied to the
  original signup workspace. Ownership loss pauses claims; ownership return
  resumes them. Deletion does not move the allowance elsewhere.
- Dismissal is remembered; Credits reopens rewards. No expiration or campaign
  budget cap. The UI counts available actions and avoids a five-action promise.
- Shared per-account throttling (30 requests/minute by default) is independent of
  generic request limiting. Counter outages fail closed; reconciliation stays
  available. This prevents request flooding, not multi-account farming.
- Preserve the existing disabled-signup and noneligible-account behavior.

## Explicitly excluded

The new receipt monitoring module, scheduled-job setup, index and monitor tests
are removed. No new scheduler, monitoring identity, notification destination or
human monitoring rota is required. Existing application logs and optional growth
telemetry remain; neither is represented as an active fraud monitoring service.
Analytics worker/dashboard deployment and QA exclusions are deferred. Reuse the
existing guarded growth deployment workflow later; do not add a new framework.

## Validation and release

1. Focused tests must cover four-action completion, unavailable YouTube claims,
   adding YouTube later without exceeding the cap, retries and shared throttling.
   Browser checks must cover dynamic action counts and completion.
2. Run full lint, typing, tests, coverage, database conformance and applicable
   billing regression gates in CI. Large local installations are intentionally
   avoided. Focused local checks do not replace those gates.
3. Check current main compatibility and exact-commit CI before merging. Deployment
   uses existing reviewed release workflows; an hour is a target, not permission
   to bypass failing or queued gates.
4. Verify signup, Credits, API-key creation, welcome behavior and a controlled
   reward claim after release. Rewards are enabled by deployment code; disabling
   them also requires a code rollout. Wider console regressions require the
   existing application rollback, not just the rewards flag.

Production deployment remains separate from committing this reduced scope.
