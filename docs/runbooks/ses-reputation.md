# SES reputation response

TrustedRouter sends transactional mail only. It does not have a bulk or
campaign-mail path. Do not add one or raise outbound volume while either the
seven-day hard-bounce rate is at or above 2% or the complaint rate is at or
above 0.1%.

## Sending classes

| Class | Sender profile | Purpose |
| --- | --- | --- |
| `email_verification` | `auth` | User-requested email verification |
| `activation_10m`, `activation_24h` | `onboarding` | Post-signup activation reminders |
| `budget_alert` | `alerts` | Workspace spend alert |
| `support_inquiry` | `support` | First-party support form to the support inbox |
| `partner_inquiry` | `partners` | First-party partner form to the operator inbox |

Every profile has a dedicated SES identity, configuration set, DKIM keys, and
custom MAIL FROM domain:

| Profile | Sender | Configuration set |
| --- | --- | --- |
| `auth` | `accounts@auth.trustedrouter.com` | `trustedrouter-auth` |
| `onboarding` | `hello@onboarding.trustedrouter.com` | `trustedrouter-onboarding` |
| `alerts` | `alerts@alerts.trustedrouter.com` | `trustedrouter-alerts` |
| `support` | `support@support.trustedrouter.com` | `trustedrouter-support` |
| `partners` | `partners@partners.trustedrouter.com` | `trustedrouter-partners` |

Each configuration set requires TLS, enables reputation metrics, suppresses
hard bounces and complaints, and publishes bounce/complaint events to the
shared verified SNS handler. Each custom MAIL FROM domain rejects on MX
failure. Account-level SES reputation is still shared across sender identities
in the AWS account, so this split improves attribution and containment but does
not create five independent SES reputations.

Every send carries privacy-safe SES message tags for `mail_class`,
`sender_profile`, `acquisition_source`, `acquisition_medium`, and
`acquisition_campaign`. Missing attribution is `unknown`. Raw recipient
addresses and message content must never be added to logs or metric tags.

## Immediate response

1. Confirm the account and all five configuration sets suppress `BOUNCE` and
   `COMPLAINT` recipients.
2. Keep every `trustedrouter-{auth,onboarding,alerts,support,partners}`
   configuration set's reputation metrics enabled.
3. Do not delete a suppression entry unless the recipient has independently
   confirmed control of the address and requested another send.
4. Group `email_send.accepted` and `ses_feedback.received` by mail class and
   acquisition source in Axiom. Compare feedback counts to accepted sends for
   the same window.
5. Pause the offending acquisition source, mail class, or sender profile if it
   exceeds the recovery thresholds. Authentication must remain available when
   onboarding or public-form mail is paused.

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
