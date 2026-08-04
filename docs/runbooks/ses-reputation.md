# SES reputation response

TrustedRouter sends transactional mail only. It does not have a bulk or
campaign-mail path. Do not add one or raise outbound volume while either the
seven-day hard-bounce rate is at or above 2% or the complaint rate is at or
above 0.1%.

## Sending classes

| Class | Sender profile | Purpose |
| --- | --- | --- |
| `email_verification` | `default` | User-requested email verification |
| `budget_alert` | `alerts` | Workspace spend alert |
| `support_inquiry` | `default` | First-party support form to the support inbox |
| `partner_inquiry` | `default` | First-party partner form to the operator inbox |

The `alerts` profile sends as `alerts@alerts.trustedrouter.com` through the
`trustedrouter-alerts` configuration set. It has Easy DKIM, a custom MAIL FROM
domain that rejects on MX failure, DMARC quarantine, required TLS, and separate
reputation metrics. Account-level SES reputation is still shared across sender
identities in the AWS account.

Every send carries privacy-safe SES message tags for `mail_class`,
`sender_profile`, `acquisition_source`, `acquisition_medium`, and
`acquisition_campaign`. Missing attribution is `unknown`. Raw recipient
addresses and message content must never be added to logs or metric tags.

## Immediate response

1. Confirm account and both configuration sets suppress `BOUNCE` and
   `COMPLAINT` recipients.
2. Keep `trustedrouter-default` and `trustedrouter-alerts` reputation metrics
   enabled.
3. Do not delete a suppression entry unless the recipient has independently
   confirmed control of the address and requested another send.
4. Group `email_send.accepted` and `ses_feedback.received` by mail class and
   acquisition source in Axiom. Compare feedback counts to accepted sends for
   the same window.
5. Pause the offending acquisition source or mail class if it exceeds the
   recovery thresholds. Authentication and billing behavior must continue even
   when best-effort alert email is paused.

AWS documents account-level suppression and automated configuration-set
pausing here:

- https://docs.aws.amazon.com/ses/latest/dg/sending-email-suppression-list.html
- https://docs.aws.amazon.com/ses/latest/dg/monitoring-sender-reputation-pausing.html

## Recovery gate

Do not scale outbound volume until all of these are true:

- seven complete days below 2% hard bounces;
- seven complete days below 0.1% complaints;
- no unexplained or unclassified feedback events;
- DKIM and custom MAIL FROM remain `SUCCESS` for both active domains;
- the suppression webhook is receiving and classifying test events.

At low volume, percentages are noisy. Report both the numerator and denominator
and do not describe one event in a tiny sample as a stable rate.
