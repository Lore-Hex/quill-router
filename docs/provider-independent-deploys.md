# Provider Health And Deployment Gates

Upstream model availability is a separate operational signal from release
correctness. A provider outage must not be treated as a broken router release.

## Control Plane

- CI runs offline routing, expiry rejection, billing, fallback and privacy tests.
- Time-sensitive catalog counts run in `Provider catalog health`, independently
  of release CI. Failures stay visible in that workflow.
- The deployment watchdog selects `router_core` or `control_plane`. Its older
  `current.checks` fallback filters to the same probe families, rather than
  folding provider inference into router availability. Unclassified probe types
  cannot supply a healthy router signal.
- SDK probes retain explicit gateway `error.source` as `provider_error` or
  `router_error`, alongside the original HTTP status. Unclassified failures
  remain failures. Error text, prompts and outputs are not added to samples.

## Attested Gateway

The companion change in `quill-cloud-proxy` consumes this classification.
Fresh chat/Responses samples explicitly attributed to a provider do not block
the immediate regional gate. They remain down on the public inference status
component. TLS and attestation must still pass; each monitor must still report
fresh samples. Transport failures and errors without explicit attribution are
not presumed to be upstream outages.

Per-instance probes distinguish an exact PONG from an explicit provider error.
Either way, the streaming request must supply valid durable authorization
evidence. Required boot-key binding and usage-heartbeat evidence are unchanged.
An uncommitted settlement/refund is a billing failure, not an excusable provider
failure. Provider errors never waive that check.

## Release Order

Deploy the control-plane classification first, then the gateway gate consumer.
Old monitor samples remain conservatively unclassified until fresh probes run.
There are no production error injections or paid provider calls in these
regression tests.
