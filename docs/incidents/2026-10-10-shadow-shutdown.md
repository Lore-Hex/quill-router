# Shadow counter shutdown failure

## Evidence

Bounded Cloud Logging reads on October 10 found repeated Cloud Run shutdown
tracebacks in Europe and South America between 06:10 and 07:12 UTC. The final
shadow counter write raised Spanner `DeadlineExceeded`, then its failure
recorder raised `ValueError: closed shadow day`. The latter escaped the
FastAPI shutdown hook. The checked Cloud Run request logs from 00:00 UTC
through the investigation contained no HTTP 5xx entries. This bounds the
observed impact; it is not proof that every public request path was healthy.

Monitoring alert-policy reads were denied to the available operations
identity. No monitoring IAM permissions or alert thresholds were changed.
Normal enclave access messages written to stderr also appeared with ERROR
severity; their HTTP 200/status-zero fields are not evidence of HTTP failures.

## Root Cause

`Counters.snapshot(closed=True)` intentionally seals the local bucket before
performing I/O, preventing late request work from reopening the writer. If
the final write fails, `Runtime.flush` called the ordinary request-oriented
`reason` method. That method requires an open bucket and rejects the sealed
one. The error handler therefore replaced a bounded observer storage failure
with a shutdown exception. Executor cleanup was not in a `finally` block.

The shadow store's existing 200 ms transaction cap can also expire on
cross-region writes. This repair does not increase that cap, change retries,
alter billing transactions, or claim to repair observer evidence completeness.
Durably unclosed writers must continue to block a clean shadow-evidence window.

## Repair And Verification

Record flush failure against the retained local bucket, even when it is
sealed, without reopening request accounting or acknowledging the failed
write. Preserve the original gap timestamp, and advance a sealed bucket's
sequence so a stale snapshot acknowledgment cannot discard the new gap.
Always release the executor on shutdown, while allowing unexpected shutdown
errors to propagate.

Regression tests reproduce the final-write timeout with and without a prior
gap, prove the durable writer remains unclosed, reject stale acknowledgments
and late request accounting, and check cleanup after an unexpected failure.
The existing rollover, retention, accounting, and shadow-proof tests remain
required. Deployment and regional verification must be recorded separately;
a local pass is not a production release.
