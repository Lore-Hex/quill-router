# Retired billing revision receiving requests

## Evidence

The billing-path 5xx alert opened at 2026-10-22T21:22:07Z for
`trusted-router-01676-tmt` in `us-central1`.

- Cloud Run reported 100% traffic on `trusted-router-01677-7ns` and RoutesReady
  at 21:21:13Z.
- The old containers received SIGTERM at 21:21:19Z, completed the existing
  three-second listener drain at 21:21:22Z, and the revision became Retired at
  21:21:24Z. Its revision-level minimum was two, but retirement removed that
  capacity.
- Five request logs were subsequently assigned to the retired revision:
  settle at 21:21:59Z, heartbeat at 21:22:23Z, authorize at 21:24:35Z, settle
  at 21:24:37Z, and authorize at 21:30:54Z. All had HTTP 500, latency `0s`,
  no container instance label and no matching application exception.
- A sampled load-balancer log for the second settle reported
  `response_sent_by_backend`. The serverless NEG targets the service, not a
  revision or tag. Service spec and status both targeted only the new revision.
- The heartbeat's gateway sent a successful heartbeat to the new revision
  about 454 ms later. The other failures cannot yet be mapped conclusively to
  customer outcomes from the available request metadata. Do not infer that
  every request recovered or that no customer was affected.

The two observed source addresses belong to the US West GCP gateway and an
Azure address. These are infrastructure sources, not customer identities.

## Diagnosis and limits

This is a post-retirement routing/availability failure, distinct from the
earlier 8-second edge versus 20-second transaction deadline incident. The
rollout immediately removed the old revision's last traffic reference at
100%. Its minimum-instance setting therefore did not preserve warm capacity.
The SIGTERM listener drain operates for seconds; it cannot cover requests
assigned to a retired revision nearly ten minutes later.

The exact Google-internal reason requests still referenced the retired
revision is not proven. Keeping it warm is a targeted mitigation, not proof
that an internal routing defect is fixed. Preserve the request traces and
retirement timestamps for Cloud Run support if the behavior recurs.

## Repair

The staged rollout verifies a `staged-drain` tag on the old revision before
100%, requires a positive immutable revision-level minimum, and retains it
through a 15-minute health watch. Only then does it remove the tag and verify
removal. The drain uses the pre-cutover baseline, never a fresh post-cutover
baseline that could hide a new failure. Secondary job time includes all three
serial drains.

An interrupted or ambiguous cutover retains the warm revision and fails
loudly. Successful rollback restores old traffic before clearing the drain.
A subsequent rollout cannot overwrite a leftover drain for a different
revision. Normal completion does not accumulate old warm revisions.

No billing retry policy, money calculation, IAM role, ingress setting, alarm
threshold or SIGTERM limit is changed.

## Verification and recovery

Regression cases cover ordering, missing minimum capacity, wrong tag
readback, health rollback, signal interruption, ambiguous traffic updates,
cleanup failure and invalid drain configuration. The initial ten tests were
run against the old script and all failed for the expected missing behavior.

Release acceptance still requires observing the protected production switch,
confirmed tag removal after the watch, and a fresh billing 5xx query covering
both revisions. Until then, call this tested remediation, not verified closure.

If interrupted, inspect service traffic and the tagged revision. If restoring
the old revision, verify it has 100% traffic before removing `staged-drain`.
If retaining the new revision, complete a fresh 15-minute healthy drain before
removing the tag. Use the deployment mutex and a deployment-capable identity.
Never shorten the grace or suppress the billing alarm to complete a rollout.

## Other alerts checked

The earlier Spanner API-failure incident resolved at 18:32:52Z; its original
cause is not established by this investigation. The CIS firewall-change alert
correlates with deletions of Lightning/BTCPay firewall rules by the signed-in
operator identity at 12:17-12:18Z. That is separate from billing retirement,
and the security alert correctly reported the changes.
