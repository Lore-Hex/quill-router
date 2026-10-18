# Social rewards operations monitoring

This monitor reads GCP issuer grant receipts independently of marketing attribution,
browser cookies and optional conversion logs. It never changes credits, admission,
or the feature flag. Thresholds only alert; there is no campaign budget cap.

## Source and completeness

Apply `scripts/deploy/social_rewards_monitor_index.sql` through the reviewed schema
release process before enabling the job. Existing payment/adverse indexes begin
with provider and cannot bound campaign reads. The new covering index starts with
`event_id`; the monitor reads only `[social-rewards-v1:, social-rewards-v1;)`.
It never falls back to scanning the table or entity JSON. Index provisioning is
not performed by the monitor and must complete before its first production run.

Each invocation uses one strong-snapshot, LOW-priority read with a ten-second
deadline, no RPC retries, and a maximum of `row-limit + 1` rows. Default budget is
5,000 receipts; maximum configurable budget is 10,000. An extra row causes an
incomplete failure, with **no partial totals**. A missing index also fails. Budget
exhaustion is an operator alert, not grounds to silently increase scan load. Before
approaching this bound, replace the full-prefix reconciliation with a reviewed
commit-ordered feed and reconciliation design. Do not add an event-time watermark:
late commits can have older `recorded_at` values.

Every run restarts at the prefix, so a receipt committed after a snapshot appears
on a later run even when its timestamp is old. Aggregate rows are snapshots and
must never be summed across runs. Identical receipt replays are deduplicated;
conflicting receipt IDs fail the run. The 15-minute velocity window uses receipt
recording time, not commit time; late commits remain in total issuance but can
fall outside that window. Monitoring is not proof that separate accounts represent
separate people and cannot itself prevent farming.

## Reviewable job contract

Use the existing control-plane image's Python runtime for a separate scheduled
job. The monitor is in `src/trusted_router`, which the application Dockerfile
copies and installs; no unbundled `scripts` module is required. Schedule every five minutes, one task, one concurrent execution, zero retries,
and a 60-second job deadline. Use a dedicated read-only database identity plus only
the logging permissions needed to publish stdout. Do not use application write
credentials. Retain a single GCP issuer job; other-cloud totals are not covered.

Command (substitute reviewed deployment identifiers and chosen alert threshold):

```sh
python -m trusted_router.social_rewards_monitor \
  --project PROJECT --instance INSTANCE --database DATABASE \
  --window-minutes 15 --alert-claims ALERT_THRESHOLD \
  --row-limit 5000 --qa-account-hashes /config/qa-account-hashes.json
```

`--alert-claims` is required: choose a threshold against expected signup volume and
operator response capacity. It does not stop grants. The optional QA file is a JSON
list of SHA256 hashes of `tr-account:` followed by the enrolled user ID, matching
the existing account-fingerprint convention. Raw IDs and emails are rejected as
configuration; receipts and IDs are never printed. QA receipts are excluded only
from `customer_claims`, not issuance totals or alert evaluation. This exclusion is
specific to this operational report; it does not configure the marketing dashboard.

Outputs:

- `social_rewards.monitor_completed`: complete snapshot, observed timestamp,
  totals, unique enrolled accounts, amounts, per-action counts, QA/customer counts,
  recent claims, and alert codes. Exit 0 for no alerts, exit 2 for alerts.
- `social_rewards.monitor_failed`: `complete=false` and error class only, exit 1.
  Missing data must never be interpreted as zero issuance.

Configure operational notifications against **this job's** log stream for a nonempty
`alerts` array, any `monitor_failed`, any job execution failure, and absence of a
completed observation for 15 minutes. Group repeated notifications by alert code;
notify once on transition and on recovery, not every healthy run. A log-only failure
monitor cannot detect a missing schedule: the independent no-data alert is required.
Existing `scripts/axiom_growth/alerts.py` watches marketing freshness and does not
satisfy this requirement. Notification destination and scheduler/IAM configuration
must be verified during rollout; neither is provisioned by this change.

## Release verification

1. CI runs unit tests plus `tests/conformance/test_social_rewards_monitor_native.py`
   in its native Spanner job (`-k spanner-emulator`). The test uses an isolated
   database, canonical receipt schema, the actual index DDL and Read RPC; it checks
   prefix exclusion, cap overflow, attribution independence and late commits.
   Verify production job read-only permissions in staging before release.
2. Install the reviewed index and job/notifications; first confirm a complete
   snapshot and the observation timestamp. Do not manually scan production entities.
3. Use an explicitly identified QA enrollment for a controlled claim. Confirm one
   receipt/30,000 microdollars, a retry without an increase, and QA classification.
   A test without acquisition attribution must still be counted.
4. Exercise a safe non-production low alert threshold, failed read, and missed
   schedule; verify notifications and recovery. Do not manufacture production grants
   or alter customer credits to test alerts.
5. Continue the existing marketing rollout from `onboarding-attempts.md`: source
   filter, worker, dashboards, freshness. Claims in that funnel are optional observed
   conversions and are not the receipt total; label their coverage accordingly.

Pending deployment: index, job identity/schedule, threshold, QA hashes, notification
route, no-data alert, actual Spanner-index verification and live delivery checks.
No production resources are created or changed by adding these files.
