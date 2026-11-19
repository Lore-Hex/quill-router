# Private enterprise request storage

The buyer deck and security pack collect an address for enterprise follow-up.
The private record contains the normalized deliverable email (not a hash), a
request UUID, UTC time, request type, and canonical site. It identifies a
request, **not** a completed download, verified mailbox owner, account, or
newsletter subscriber. A repeated form submission is a new request. A queue
retry is the same request. No name guessing, newsletter enrollment, attribution
IDs, free text, URLs, or campaign parameters in this table.

The existing anonymous delivery events and tracking opt-outs stay unchanged.
The optional capture is after input, origin, honeypot, rate and asset checks
and before SES. A queue failure gives a retryable 503 without sending mail or
granting a download. An SES failure can therefore leave a genuine request but
no delivery. ClickHouse unavailability does not block the form while the queue
is healthy. There is no subscription to email, Gmail or Axiom in this design.

## Activation order

1. Apply the reviewed Terraform plan for `infra/enterprise_requests.tf` using
   the normal protected infra workflow. Check inherited permissions as well as
   topic/subscription IAM; no general analytics or CRM credential should be
   able to consume messages. Only the control-plane form service needs publish.
   The private subscription retains unacknowledged messages for seven days.
2. Apply `clickhouse/029_private_enterprise_requests.sql` on the replicated
   production cluster. Review effective grants, including inherited roles:
   analytics, Axiom and public readers must not be able to select
   `tr_enterprise.requests`. Do not include this database in shared analytics
   exports or their backups. Check that ordinary analytics readers also lack
   access to query/trace logs containing other users' data.
3. On the chosen GCP ClickHouse node provision a **localhost-only** SQL user
   `tr_enterprise_insert` with only INSERT on `tr_enterprise.requests`.
   Place its generated password in root-owned mode 0600
   `/etc/tr-enterprise/insert.env` as `TR_ENTERPRISE_INSERT_PASSWORD`; do not
   reuse an admin password. Install the release project and locked runtime
   dependencies in `/opt/tr-enterprise/venv` and the `clickhouse/` package in
   `/opt/tr-enterprise`. Install the matching timer and service and enable
   `tr-enterprise-request-ingest.timer`. The service uses the node identity for
   pull/ack, ignores HTTP proxies for the local insert and requires two replicas
   before acknowledgement. A retry reads as one logical row with `FINAL`.
4. Publish one synthetic record from the publisher identity. Verify the worker
   succeeds, a restricted operator can retrieve the exact test email by UUID,
   the ordinary analytics identity cannot, and the backlog drains. Remove the
   test row by UUID. Only then set `TR_ENTERPRISE_REQUEST_CAPTURE_ENABLED=true`
   on the form service through normal deployment. Smoke-test the real form
   with an operator-controlled address; never use a customer as a test.
   Roll back by disabling capture on that service; keep draining queued records.

## Retention, erasure and CRM

Raw addresses expire after 180 days at ClickHouse merges. Do not wait for
TTL to service an erasure request: after processing any outstanding submissions,
use a restricted operator session to find the matching request UUIDs (bind the
email privately; do not put it in shell history or logs). Delete by UUID across
replicas with a synchronous mutation and verify zero matches on each replica.
Reconcile the enterprise inbox and any authorized CRM copies separately. Exclude
erased records from any future historical import or restore.

An authorized CRM worker needs SELECT on this table only, not general database
or inbox access. Read with `FINAL`, use request UUID as the external event ID,
match contacts by email, and merge request tags without replacing existing ones.
A CRM sync and historical email backfill are separate steps, not activated by
this deployment. MARKET_DATA and SAMPLE_DATASET are reserved types; only publish
them from a verified intake source, never from a page view.
