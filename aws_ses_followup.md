# AWS SES Sandbox-Exit Reply

**Don't send blindly — review and paste into the AWS Support case yourself.** Trim or expand any section so it reflects exactly what you've shipped.

---

Hi AWS Support team,

Thanks for the reply. Below is a complete picture of how we plan to use Amazon SES.

## Use case

We use SES exclusively for **transactional account-verification email** on TrustedRouter (https://trustedrouter.com), a hosted, attested LLM API service. The email volume is **low and one-shot**: when a user signs in for the first time using MetaMask (which has no email associated), they enter an email address and we send a single magic-link message to confirm the address. After that one click, no further mail is sent to that recipient. Users who sign in via Google or GitHub do not receive any email from us at all — those identity providers already verify the email at the OAuth layer.

Expected volume: **dozens of messages per day during alpha**, growing to a few hundred per day at general availability — strictly tied to first-time wallet sign-ups, not marketing.

## Sending domain and authentication

- Verified identity: `trustedrouter.com` (verified in `us-east-1`).
- Easy DKIM is enabled (CNAMEs published in Cloudflare).
- Custom MAIL FROM domain `mail.trustedrouter.com` configured (MX + SPF TXT records published).
- DMARC record published.
- All sends originate from `noreply@trustedrouter.com` with display name `TrustedRouter`.

## Recipient lists

We do not maintain recipient lists. Every send is initiated synchronously by the recipient's own action of entering their email at sign-up. There is no mailing list, no marketing campaigns, no scheduled sends.

## Bounce, complaint, and unsubscribe handling

Each verified identity send routes through the `trustedrouter-default` SES configuration set, which has an event destination of type SNS pointing at the `ses-feedback` topic in `us-east-1` (ARN: `arn:aws:sns:us-east-1:330422590279:ses-feedback`).

- **Bounces**: Hard bounces and complaints are emitted to that SNS topic. We have an HTTPS subscriber at `https://trustedrouter.com/internal/ses/notifications` that verifies SNS message signatures (SignatureVersion 1 SHA-1 and SignatureVersion 2 SHA-256), parses the SES bounce envelope, and adds the recipient address to a per-account suppression list. Permanent bounces stop further sends to that address; transient bounces (mailbox-full, greylisting) are not suppressed. The suppression list is consulted before every `SendEmail` call.
- **Complaints**: Treated identically to permanent bounces — the address goes onto the suppression list immediately and is never sent to again.
- **Unsubscribe**: The verification message is one-shot (a single sign-up confirmation), not subscription-based. The body links to the recipient's account where they can delete the account and all associated data.

We have built and deployed end-to-end signature verification, suppression-list update, and idempotency handling for SNS message replays before requesting the limit increase.

## Sample message body

> **Subject:** Confirm your TrustedRouter account
>
> Welcome to TrustedRouter.
>
> Click this link to confirm your email address and finish creating your account:
>
> https://trustedrouter.com/auth/verify-email?token=…
>
> The link expires in 24 hours. If you didn't try to create an account, you can ignore this email.

There are no images, no attachments, no marketing content, and no tracking pixels. The HTML body is the same plain message wrapped in `<p>` tags.

Please let me know if there is any other detail you need.

Thank you,

Joseph Perla
TrustedRouter
