# Authorization edge deadline mismatch

## Observed failure

At 2026-10-22T03:23:44Z, the global load balancer returned HTTP 504 with
`statusDetails=backend_timeout` after 8.004314 seconds for an internal gateway
authorization. The selected backend was `trusted-router-control-backend`, not
the fast-path backend. The same trace later recorded HTTP 503 from the Python
billing handler after 20.019515 seconds.

The exact authorize route used weighted backends without an explicit route
timeout. The control backend had a 30-second timeout, the fast-path backend an
8-second timeout, and the fast-path traffic weight was zero. The observed edge
deadline was incompatible with billing's 20-second work budget. The precise
Google load-balancer behavior selecting the shorter timeout is not established;
the same-trace edge/backend mismatch is established.

The enclave retried the early 504s while earlier billing attempts could still
be working. Lock statistics showed overlapping authorization transactions on
the same idempotency and credit rows. One customer request eventually succeeded
after three authorization attempts and about 24 seconds in authorization.
Two settlement records investigated during the incident had subsequently
reached their settled state. This is not a full financial reconciliation.

## Repair

Set the exact `/internal/gateway/authorize` route's explicit timeout to 24
seconds. This allows billing's 20-second work budget plus up to two seconds of
cleanup to return before the edge cuts off the request, while remaining below
the enclave's 25-second response-header deadline.

Do not change the fast-path traffic weight, billing deadline, credit limits,
attestation policy, or alert thresholds. This is separate from disabling the
shadow-only observer: that containment cannot repair the edge timeout.

The regression test fails against the previous route configuration and requires
the explicit deadline. Validate the Terraform plan contains only this URL-map
change before applying. Read back the deployed route and check new edge 504s
and billing 5xx afterward; successful health checks alone cannot prove the
slow-request case is repaired.

## Remaining investigation

The initial database slowdown is not fully explained by this mismatch. CPU
metrics did not indicate instance-wide capacity exhaustion. The gateway's
separate two-second streaming settlement handoff can also overlap work that
continues on the billing service; its ownership/retry behavior needs a separate
review. Neither issue should be called resolved by this route change.

A preceding stream had a usage-cap rejection and requires separate diagnosis.
Do not increase a customer's caps as a substitute for identifying its cause.
