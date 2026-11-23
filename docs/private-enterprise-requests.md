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

The Twenty job consumes a separate subscription, not this table or the inbox.
Only verified submissions belong here: MARKET_DATA and SAMPLE_DATASET are
reserved for a verified intake or reviewed import, never inferred from page views.
The job matches exact primary/additional emails, compares updatedAt before
adding a tag, and does not overwrite names, other fields, or other tags.
On ambiguous identity or transient errors it leaves the message unacknowledged.
It sends no email and never enrolls anyone in a campaign.

## Twenty activation

1. Apply the reviewed infrastructure. Check the CRM job identity can consume
   only its subscription and access only the dedicated secret. Do not grant it
   ClickHouse, Gmail, Axiom, or billing access. Record backlog alert delivery.
2. In the correct Twenty workspace provision a People-only API-key role (read,
   create/update; no delete/settings). Confirm its assignment and store the
   credential as a version of `tr-enterprise-twenty-key`, outside Terraform.
   Never paste the key into the repository, command arguments, tickets or chat.
3. Run `bash scripts/deploy/enterprise_twenty_job.sh IMAGE@sha256:DIGEST VERSION`
   with the verified production control build containing this module. It
   installs a dedicated Cloud Run job without enabling a new timer.
4. Publish a controlled test submission, execute the job, and verify the
   tagged person in Twenty. Replay the same message and then a different
   request type for that address; verify **one** person with both tags.
   Confirm only email and request tags were sent and the backlog drained.
   A ClickHouse acknowledgement is not evidence of a CRM write.
5. Re-run the script with the same arguments and `--verified-canary` to start
   the five-minute authenticated scheduler. Verify two scheduled completions.
   Pause that scheduler to roll back CRM independently; do not purge either
   subscription. During erasure, reconcile pending CRM messages before removing
   tags/contacts as required. Historical imports require their own reconciliation.
