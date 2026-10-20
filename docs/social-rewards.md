# Social rewards

Social rewards offer optional promotional API credit to eligible new direct
Google/GitHub signups. Users can start using the API immediately; completing a
request or a social action is not required for access.

## Credit and eligibility

Enrolled accounts receive 150,000 microdollars at signup and can earn 30,000 per
available action. The four available actions are starring the repository on
GitHub, following TrustedRouter and its founder on X, and following TrustedRouter
on LinkedIn. Completing all four brings the campaign total to 270,000 microdollars.
A configured official YouTube channel adds a fifth action, bringing the maximum
to 300,000 microdollars ($0.30). These are dollar credits, not a guaranteed number
of tokens; token usage depends on model pricing.

Existing accounts are not enrolled retroactively. Existing signup exclusions
continue to apply, including delegated signups and accounts that do not qualify
for starter credit. Accounts created while rewards are disabled retain the normal
signup-credit behavior and are not retroactively enrolled when rewards return.

The allowance belongs to the account and its original signup workspace. Switching
workspaces, spending credit, or repeating an action does not reset it. Losing
ownership pauses remaining rewards; regaining ownership restores access. Deleting
the original workspace leaves the remaining rewards unavailable. Rewards have no
expiration date.

## User experience

Rewards are available immediately during onboarding and later from Credits. Users
can dismiss the offer and return without repeated automatic popups. The offer must
not cover the one-time API-key reveal.

Each action opens its social destination and requests the credit. This does not
verify a follow or star, require account linking, or prove social engagement.
Already granted actions remain completed. Missing or invalid YouTube configuration
hides that action and rejects direct claims for it; the other actions remain usable.
Adding YouTube later makes it available to existing enrolled accounts too.

A pending claim has a bounded recovery period, with retries and status checks
sharing a 15-second budget. An uncertain response shows a retryable state rather
than an indefinite loading indicator. Repeating a claim cannot issue additional
credit for the same account and action, including after a lost response.

## Availability and safeguards

The campaign is enabled in deployment code on the primary GCP deployment only.
Other clouds do not issue independent campaign allowances. Disabling rewards
hides the offer and prevents new enrollment and claims while preserving credits
already granted and saved progress for enrolled accounts. Changing the switch
requires a code deployment; it is not an instant operational toggle.

Claims have a shared per-account request limit, normally 30 per minute. Excess
requests are rejected temporarily. If that protection is unavailable, new claims
are refused until it recovers; status reconciliation remains available. This
limits request flooding, not farming through multiple accounts. There is no
campaign-wide budget cap or verified-person identity check.

Optional engagement analytics must not block onboarding or credit grants. A
recorded click is not a verified follow, and analytics are not the source of truth
for granted credit. There is no dedicated fraud-monitoring service or human
monitoring requirement attached to this feature.
