# Self-service account closure

Customers can open https://trustedrouter.com/console/account/preferences and
select **Delete account**, or go directly to
https://trustedrouter.com/console/account/delete after signing in.

The confirmation explains that this disables access, not personal-data erasure.
It requires an active cookie session, same-origin POST, a session-bound CSRF
token, the exact word `DELETE`, and a separate acknowledgment checkbox. An API
key cannot close an account, and the form cannot select a different user.

One database transaction marks the user disabled with `account_closed_at`,
archives their solo workspaces through the existing lifecycle operation,
disables workspace keys (including delegated app keys), and turns off auto
refill. All sessions are rejected using the strongly read user state. Identity
indexes remain as tombstones so OAuth sign-in cannot recreate the account or
grant another signup credit. In-flight requests and payments can still settle;
balances, ledger records, payment references and usage metadata are preserved.
No hot-path API lookup or schema migration is added.

Shared-workspace membership, shared ownership, inconsistent owner inventory,
more than 16 workspaces or more than 128 keys require support. Validation runs
before mutation; failure must leave the account open. Ownership transfers or
leaving a team must be arranged without disrupting other members. This operation
applies to the account on the current control plane; independently created
accounts on other planes require separate handling.

Federated API-key copies have a 15-minute soft refresh and a 24-hour hard
expiration. A healthy peer rejects the key at its next refresh; an unreachable
home plane can extend cached access until that hard expiration. The confirmation
and receipt disclose this delay. Do not promise immediate cross-cloud revocation;
urgent requests require targeted revocation on each receiving plane as well.

## Privacy requests are separate

Disabling access does **not** complete an Article 17 erasure request. Do not tell
customers their data has been erased based on this screen, and do not require
customers who already emailed a valid erasure request to submit it again.
Track existing requests through the privacy process. Inventory personal data
across the operational store, analytics, processors, replicas and backups;
erase eligible records and document any specific retention basis and duration
before sending a completion notice. Closure does not automatically email anyone.

## Verification

- Run `tests/test_account_closure.py` and the backend-neutral
  `tests/conformance/test_account_closure.py` on memory, native Spanner and
  PostgreSQL.
- Confirm old cookies, session bearers and API keys fail after closure, OAuth
  cannot reopen it, unrelated accounts still work, and monetary totals do not
  change.
- Run `tests/browser/account-closure.spec.js` at desktop and mobile sizes.
- Production: verify the sign-in redirect and the authenticated confirmation
  screen. Do not close a customer's or operator's real account as a smoke test.
