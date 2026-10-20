# Notices sender authorization

`infra/ses_notices.tf` configures the verified notices identity, DNS and
configuration set. That does not authorize the existing send-only IAM user.
The separate operator-managed policy below is required as well.

On October 18, 2026, the retirement preview failed with SES `AccessDenied`:
`trustedrouter-ses-sender` could send sign-in mail and operational alerts,
but not mail from `notices.trustedrouter.com`. DKIM, identity verification,
and custom MAIL FROM were already successful. The narrowly scoped policy
was applied and IAM evaluation changed from denied to allowed for the exact
sender and configuration set. Alternate From addresses remained denied.

## Repair

Use an authorized AWS operator in account `330422590279`, not the GCP ops
identity or the GitHub deploy role. The latter intentionally lacks IAM-user
policy writes. Do not broaden CI permissions merely to manage this policy.

```bash
aws sts get-caller-identity
aws sesv2 get-email-identity --region us-east-1 \
  --email-identity notices.trustedrouter.com
aws iam put-user-policy --user-name trustedrouter-ses-sender \
  --policy-name TrustedRouterSesNoticesSendOnly \
  --policy-document file://scripts/ops/policies/ses-notices-sender.json
```

Do not replace `TrustedRouterSesSendOnly`: it owns sign-in and alert mail.
The notices policy grants only `ses:SendEmail`, only the notices identity and
configuration set, and only `notices@notices.trustedrouter.com` as the From
address. It does not grant raw-email sending or wildcard sender permissions.

## Verify

Read the policy back and compare it to the checked-in JSON. Simulate
`ses:SendEmail` for both exact resource ARNs with the `ses:FromAddress` context
set to `notices@notices.trustedrouter.com`. Both resource decisions must be
allowed. Repeat with `anything@notices.trustedrouter.com`; both must be denied.

Check the next application `email_send.accepted` event for profile `notices`.
IAM simulation proves authorization, not delivery. Leave retirement notices
in preview mode until the separately reviewed customer rollout. A failed
preview consumes that day's deduplication marker; do not delete production
markers or send to customers merely to test the permission repair. An
operator-only canary or the next day's preview can establish actual delivery.

Preserve account and application suppression, TLS requirements, feedback
destinations, custom MAIL FROM failure behavior, and all alert thresholds.
