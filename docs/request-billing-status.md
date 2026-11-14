# Request billing reconciliation

Read the durable billing outcome using the `x-request-id` / `x-tr-request-id`
returned by the gateway:

```http
GET https://trustedrouter.com/v1/requests/rlog_<32 lowercase hex>/billing
Authorization: Bearer <the inference key>
```

This is a control-plane API at `trustedrouter.com`, not the inference gateway
at `api.trustedrouter.com`. Management sessions can also read their workspace's
requests. Delegated inference keys can read only their own requests.

The response's `data` contains `gateway_request_id`, `billing_status`,
`cost_microdollars`, `currency` (`USD`), `finalization_outcome`, and
`generation_id`. One dollar is 1,000,000 microdollars.

| Billing status | Meaning |
| --- | --- |
| `pending` | The authorization exists; no final ledger outcome is available. Cost is null. |
| `settled` | A final charge exists. The amount can be zero. `reaped_snapshot` identifies recovery from the last durable usage snapshot. |
| `refunded` | This authorization finalized as refunded, with zero customer charge. This is not a later discretionary account credit. |
| `unknown` | A legacy terminal record lacks enough information to report the final amount. Cost is null. |

A 404 means no unique, authorized billing record is available for this ID.
It does **not** mean no provider ran, no reservation exists, or nothing was billed.
Older requests, legacy storage records, or orchestration parent IDs shared by
multiple authorizations may not be resolvable by this endpoint. Contact support
with the request ID and UTC window instead of assuming zero or regenerating.
Storage errors are errors, not a synthetic zero-charge result.

New gateway authorizations retain the original request ID before provider work.
An idempotent replay keeps the original ID and terminal result. This endpoint
does not query the eventually consistent activity/Observability store, scan
production rows, expose prompt/output content, or grant another workspace access.
Spanner uses the existing exact trace index; no schema migration is required.

Deploy the control-plane support before the gateway change. Validate pending,
settled, refunded, unknown, wrong-workspace and wrong-key cases in tests. A
successful build or CI run is not proof that these APIs are deployed.
