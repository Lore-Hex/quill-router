# Gateway contract warnings

Authenticated Chat Completions and Responses adapter rejections (400, 422,
501) produce a Sentry warning, not only an enclave log. Ordinary successful
key validation, invalid/disabled/expired keys, and routine 401/402/429 traffic
do not produce this warning.

The enclave appends a status, public parameter category, and generated request
ID to its existing post-response key-identity lookup. The 750 ms deadline is
unchanged. There is no extra request, billing hold, provider call, or Sentry SDK
inside the enclave. The control plane authenticates both the internal caller
and the customer key before recording a warning. Telemetry failure cannot
change the already-written customer response.

The warning includes verified workspace and credential identifiers, route,
HTTP status, parameter category, and request ID. It excludes prompts, outputs,
unfiltered parameter values, field names that are not bounded identifier
paths, raw keys, email, and raw error text.
Unknown parameter names become `other` before leaving the enclave. The receiver
also enforces that category boundary.

Each warning names the verified workspace as its Sentry user, by its
analytics surrogate (`sha256("trustedrouter:workspace:" + id)`, resolvable
through ClickHouse `tr.workspace_directory`), so an issue's user count is a
count of distinct workspaces. It is a lower bound: the local limiter passes one
event per route, status and parameter per hour per process, so the busiest
caller wins most slots and a second workspace can stay unseen for a while.
When the gateway sent a bounded identifier path, the
`rejected_field` tag carries it with item indices removed (`input[].content`),
so the tag breakdown shows which fields one issue refuses. Read the two
together: a field no contract defines, from one workspace, is that client's
mistake; many workspaces refused the same way is a gap or regression on our
side. Every rejection, sampled or not, is still in the enclave's
`enclave.request_contract_rejected` log line.

Warnings group by route, status, and parameter, not by workspace or request ID.
A separate in-memory limiter allows one event per group per hour and ten such
warnings total per hour per control-plane process. Restarting a process resets
that local budget; it is not a fleet-wide quota. The normal Sentry scrubber and
global floodgate still apply. Other server errors keep their existing policy.
Enclave request logs are retained independently of this warning sampling.

To investigate, use the warning's `request_id` to find the corresponding
`enclave.request_contract_rejected` and `enclave.request_end` metadata logs.
The verified workspace/credential IDs identify the affected account without
putting its email or credentials in Sentry. Do not retrieve request content.

Deploy the control-plane receiver before the enclave sender. Older senders
continue ordinary key validation; older receivers ignore the additive metadata
but do not emit these warnings. Verify the sender rollout before declaring the
alerting path live. SDK PONG monitoring is complementary, not a substitute for
customer-error visibility.
