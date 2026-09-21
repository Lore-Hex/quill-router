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
parameter values, arbitrary field names, raw keys, email, and raw error text.
Unknown parameter names become `other` before leaving the enclave. The receiver
also enforces that category boundary.

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
