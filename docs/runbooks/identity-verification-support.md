# Runbook — someone's identity check failed

Every attempt costs the person $5, charged when the session is created and not
refunded by Veriff. So a wrong decline is a real loss to them and a support
case for us. This is how to work one.

## What they see, and why it is vague

`/console/account/verification` shows copy from
`src/trusted_router/identity_guidance.py`, not Veriff's words:

- **resubmission_requested** → the specific reason ("There was glare on the
  document. Tilt it away from the light."). Veriff strongly advises telling
  people this, and it saves the next attempt.
- **declined** → one neutral message for every reason code, plus a checklist
  ("hold the physical document — not a photo, a screen, or a printout").

The decline text is deliberately identical for all codes. The granular reasons
are fraud detectors — 503 tampering, 504 suspicious behaviour, 505 known fraud,
515–518 the screen/printout family, 526 photos not genuine — and Veriff
publishes no end-user guidance for them. Naming the one that fired tells
whoever tripped it exactly what to change next time. The checklist still fixes
the honest version of the failure, which is the common case: someone
photographs a scan of their own passport, reads "use the physical document",
and passes on the retry.

**Do not paste the reason to the customer**, and do not tailor the message per
code. `tests/test_identity_guidance.py` enforces sameness; that test failing is
the signal that the leak has been reintroduced.

## Where an operator reads the real reason

1. **Our store, first.** `veriff_decision_code`, `veriff_decision_reason`, and
   `veriff_decision_reason_code` on the `User` row, populated by the webhook.
2. **Veriff Station** — <https://station.veriff.com>, Verifications, search the
   session id (`user.veriff_session_id`). Shows the captured images, the
   decision, and the reason. This is the console to open when the store row
   looks wrong or empty.
3. **The API**, when Station is not enough or the webhook never landed:

   ```bash
   uv run python scripts/reconcile_veriff_decisions.py --email them@example.com
   ```

   Dry run by default; prints `reason` and `reason_code` alongside the mapped
   status. Add `--apply` to write the decision the webhook missed. Veriff does
   not resend webhooks, so without this the person sits at `pending` forever
   with the fee already charged.

## Deciding

Corroborate before you extend or refuse trust. Signals worth checking:

- Does the ID name match the cardholder name on their top-up?
- Is the payment country the same as the document country?
- Is this their first attempt, or the fifth on a fresh account?
- Which document type did they use — Israel, for example, has Passport, ID Card
  and Driver's License enabled but not Resident Permit, and a person holding
  only the un-enabled type gets declined for reasons unrelated to fraud.

A single decline on an account with a consistent name, a real card, and no
velocity is much more likely a bad photo than fraud.

## Giving another try

Resetting to `none` clears the block; the next attempt charges $5 again, so
credit them if the decline was ours to own.

```python
STORE.set_user_identity_status(user.id, status="none")
```

Credit through the normal typed-counter path with a reason string that names
the case, so the movement is legible in the ledger later.

## What to say

Plain, short, no forensics: the attempt did not pass, they can try again, and
here is the checklist. If we reset and credited them, say so. Never say which
detector fired — not in email either.
