# A3-C1 process inventory source binding, version 1

Status: source protocol submitted for owner/release review in round 3. This is
not a claim that a reviewer approved these bytes or that native qualification
ran. The only production observations used are the names/counts in
`/tmp/v2-live-c1-review-1/platform-evidence.md`, dated 2026-10-13. No network or
production read occurred in this implementation round. The approved A3 design,
§15.3 and §13 Q6, controls. `ProcessCompletenessQualification` remains CLOSED
for production until the witnesses and delivery boundary below are captured.
A flag in a capture is not a new source of authority: the independently reviewed
deployment FILE_REF and release-plan digest must bind the originals and adapter.

## Decision and independent population

Bind the router to the platform-owned Cloud Run system lifecycle log, conditional on proving and enforcing one evidence-writing process per instance lifetime. Bind enclave VM lifetimes to Compute Admin Activity and retained membership of the four named MIGs. For enclave process lifetimes, qualify the existing Confidential Space launcher records in `projects/quill-cloud-proxy/logs/confidential-space-launcher`: the launcher is outside the evidence-writing workload and records workload launch and exit. Neither current revision listing, VM events alone, nor surviving writer startup records supplies the workload population. Use an additional supervisor only if the existing platform/launcher source cannot establish distinct launches, exits, restart joins and the required completeness boundary. Retain the independent outer instance lifetime in either case.

A launch exists even when it emits no application byte. Enumerate launches first;
then join startup correlations. Every unmatched start, exit, restart, ambiguous
identity, missing boundary and conflicting record remains in `deployment.expected`
and blocks `writer_roster_gap`/G2. Never invent a UUID for a launch. The roster
contains only uniquely joined boots; `complete=false` prevents that partial
roster from being mistaken for the expected population. Zero evidence, retired,
killed and overlapping boots remain expected. The retained serving shape calls
an interval endpoint `flushed_at_us`; here it is the independently observed
lifetime endpoint, **not** a claim that a writer flushed or acknowledged.

## Router adapter: cloud_run_instance

Named resources: project `quill-cloud-proxy`, service `trusted-router` in
`us-central1`, `us-east4`, `europe-west4`, `southamerica-east1`; capture the exact
service and every relevant revision resource name, including retired revisions.
Do not infer the traffic-serving region from an enclave hostname.

Read `projects.locations.services.get`, `projects.locations.services.revisions.get`
and explicitly scoped `projects.locations.services.revisions.list` (Cloud Run
v2); preserve service traffic targets, revision image/release, effective env pins,
scaling, concurrency and termination/CPU configuration. The capture adapter's
`platform` projection is `{role,revision,region,pins,grace_us}` and is reviewed
against these original pages; `revision` is the full released source SHA, not the
Cloud Run revision name. `binding.revisions` maps the latter to the former using
captured revision image/release and release/attestation records. No current-service
GET can recover a deleted historical revision; retain the rollout original.

`logging.entries.list` filter for a single UTC day `[D,D+1)`:

```text
logName="projects/quill-cloud-proxy/logs/run.googleapis.com%2Fvarlog%2Fsystem"
resource.type="cloud_run_revision"
resource.labels.project_id="quill-cloud-proxy"
resource.labels.service_name="trusted-router"
resource.labels.location="<one approved region>"
timestamp>="<D>T00:00:00Z" timestamp<"<D+1>T00:00:00Z"
```

Do not filter out unknown message kinds: they may be a previously unclassified
kill. Retain resource labels `configuration_name,location,project_id,revision_name,
service_name`, entry `labels.instanceId`, `insertId,timestamp,receiveTimestamp,
severity,textPayload`. Identity is `(project,location,service,instanceId)` in the
reviewed pass scope. Start is a text prefix `Starting new instance. Reason: `;
observed reasons include `DEPLOYMENT_ROLLOUT` and
`MANUAL_OR_CUSTOMER_MIN_INSTANCE`. The exact observed stop message is
`Shutting down user disabled instance`. `Default STARTUP TCP probe succeeded `
is corroboration, never an independent launch. `insertId` deduplicates an exact
entry, not a process; conflicting bytes for that ID block. Original event time
is `timestamp`; receipt delay is `receiveTimestamp - timestamp` and is not a
process duration. Full launch identity is the scoped instance identity **only
under the one-process qualification**. A second launch on the same identity,
unknown crash/kill kind or missing exit fails qualification. There is no guessed
regex for crash/kill: capture it in the experiment, review the exact parser
extension, then activate. Restarts use a new independently recorded instance ID
only if that behavior was proved/enforced; otherwise use supervisor launch IDs.

## Enclave outer adapter: compute_instance

MIG names: `quill-enclave-mig-us`, `quill-enclave-mig-uswest1`,
`quill-enclave-mig-useast4`, `quill-enclave-mig-eu`. The plan must supply their
exact project/zone or region resource URIs from captured configuration; the
names/counts file does not establish those URIs. Never discover other groups.
Read the named `regionInstanceGroupManagers.get/listManagedInstances` (or the
zonal `instanceGroupManagers` equivalents when the captured resource is zonal),
exact `instances.get`, and exact `instanceTemplates.get`. Preserve historical
member instance IDs and names, template versions, attested image/source release,
true zone-to-region mapping, effective pins and termination grace. A present-day
member list omits deleted VMs and cannot define yesterday's population. The
approved capture must include rollout/member history and each audit resource
name; an unknown member in the day is a gap, not an ignored row.

Use `logging.entries.list` with exact project, `cloudaudit.googleapis.com/activity`,
`protoPayload.serviceName="compute.googleapis.com"`, resource type `gce_instance`,
and methods `v1.compute.instances.insert` or `v1.compute.instances.delete`, the
explicit named instance `protoPayload.resourceName` set from retained membership
history, and the same one-day timestamp interval. Split long resource sets into
separately approved finite passes. Retain *all* operation-start/completion entries.
`operation.id`, `operation.first`, `operation.last` distinguish acceptance from
completion; only successful `operation.last=true` (no nonzero
`protoPayload.status.code`) closes a VM lifecycle boundary. Missing completion
never becomes a successful launch or delete. Preserve `insertId`, both timestamps,
`resource.labels.project_id,zone,instance_id`, `protoPayload.resourceName,
methodName,serviceName,status`, and original response/operation metadata.

VM identity is `(project,zone,numeric instance_id)`; `binding.instances[instance_id]` supplies the reviewed `resource_name,mig,revision,region` join. VM insert/delete completions are outer boundaries, not enclave process start/close acknowledgements. The candidate process source is the launcher's own `workload task started` and `workload task ended and returned N` entries; `TEE container launcher exiting` records the launcher exit separately. Retain original log/resource identity, instance ID, `insertId`, timestamps, `MESSAGE`, journald `_BOOT_ID` where present, workload return information and launcher exit information. Before use, capture exact native shapes and publish the bounded named-instance/day query and parser. Define a distinct independent launch identity for every workload start, including multiple starts within one VM boot, and an unambiguous join to the content-free writer correlation. `_BOOT_ID` alone is insufficient unless one workload per such boot is proved and enforced. Missing launch, exit, identity, original or delivery boundary remains missing. Both termination-before-correlation and same-instance-restart witnesses are required; if these cannot be supplied from the existing launcher, implement the additional supervisor protocol below.

## Enclave candidate adapter: confidential_space_launcher

This is a proposal for qualification, not an implemented native parser or an
approved source. For each retained member of the four named MIGs, including
retired members, issue this bounded `logging.entries.list` query for one named
numeric instance and one UTC day. The resource name and zone come from captured
membership, not a prefix search; the observed fastpath service is excluded.

```text
logName="projects/quill-cloud-proxy/logs/confidential-space-launcher"
resource.type="gce_instance"
resource.labels.project_id="quill-cloud-proxy"
resource.labels.zone="<retained instance zone>"
resource.labels.instance_id="<one retained numeric instance id>"
timestamp>="<D>T00:00:00Z" timestamp<"<D+1>T00:00:00Z"
```

Use 200 entries/page, 10,000/pass, 10 seconds/call, zero retries and the explicit
continuation and completeness rules below. Do not filter MESSAGE by known text.
Retain original bytes, logName, resource type and all labels, insertId, timestamp,
receiveTimestamp, severity and jsonPayload, including MESSAGE, _BOOT_ID,
SYSLOG_IDENTIFIER, _PID, _EXE, _CMDLINE when present, workload,
workload_execution_sec, exit_code and exit_msg. Missing fields remain missing.

`LauncherOriginParser` is a required qualification seam: original native shapes
must establish exact launcher process identity fields and values separately from
the wrapper used for workload stdout. Only records with that established origin
and exact launcher message shapes can enumerate a workload. MESSAGE text alone
cannot distinguish origins. The names/counts samples contain neither exact
origin values nor sufficient original shapes; no guessed parser is specified as
working. Workload JSON inside MESSAGE is decoded separately as a correlation;
it cannot create an independent launch. Classify every captured kind, including
explicitly reviewed non-lifecycle kinds, before counting lifetimes.

Proposed workload identity is `(project,zone,instance_id,_BOOT_ID,start insertId)`
with the retained start record supplying the last component. Multiple starts
within one VM boot have different start insertIds; _BOOT_ID alone cannot identify
a workload. `LauncherLaunchDescriptor` must expose that exact retained start
coordinate to the child before its correlation (or an independently retained
launcher sequence with a proved one-to-one join to it). Capture this acquisition
and its clock/ordering relationship in both witnesses. Such an acquisition is
not demonstrated by the samples and remains missing. A writer-created UUID
cannot stand in for it. Missing _BOOT_ID requires a separately qualified outer
boot join, never an empty identity. Pair workload exits with that launch identity;
`workload task ended and returned N` closes the workload observation, while
`TEE container launcher exiting` and its exit_code/exit_msg close the launcher
observation separately. Do not use launcher exit as measured workload close.

`LauncherDeliveryBoundary` must establish durable retention, exclusions/sinks,
recovery after whole-VM/launcher loss and an independently complete day boundary.
Loss between process creation and `workload task started` is part of the first
witness. If no independent record survives, use the additional supervisor for
that demonstrated gap. Neither API EOF nor observed lag resolves this seam.

## Bounds, EOF, retention and delivery

Every query names one resource scope and one UTC day. Maximum 200 records/page,
10,000 records across the whole collector pass, 10 seconds for a call including
iteration/cleanup, zero retries. CI selected run/job/artifact GETs use at most
100 records/page. SDK retries must be explicitly disabled. Every attempted call
is retained with request scope, input token, returned token, elapsed time, original
response FILE_REF, truncation/error, EOF and independently established
`complete_through_us`. A failed call's records do not count as an EOF. A short or
empty page with `nextPageToken` must continue. An absent continuation token in a
successful untruncated response ends the API enumeration; retain that response
as the terminal proof. Token repetition, omitted continuation capture, deadline,
record exhaustion or unknown message classification stays missing. Full pages
may end only when the platform explicitly omits continuation. No retry, LIST of
buckets, database body scan or query widening follows failure.

`Monitoring.projects.timeSeries.list` is limited to the exact service/revision or
VM resource IDs, one day, selected instance/CPU series and the same bounds. It
corroborates time/resource coverage and cannot establish boot completeness.
Service/revision GETs have a singleton terminal page. Source captures are an
explicit `records` wrapper around the retained parser projection/original log
entries; the untouched native response is a separate FILE_REF in the approved
capture graph. `NativeClients` must implement the adapters and preserve original
request/response metadata before production use; the offline CLI cannot connect.

Retain originals, correlation/readiness/spans, review records, pages, failed
passes and manifest dependencies for **at least 30 days**, including carry-in
and forward termination support. The evidence file does **not** establish any
bucket's retention, exclusions, sink coverage, delivery SLA, or losslessness.
Before qualification capture exact logging bucket `get`, sink/exclusion
configuration and IAM/resource scope; verify the requested retention and oldest
required day are available. Trial an expiry boundary in isolated resources,
not by deleting production evidence. Record measured ingest lag over planned
fault cases and a bounded post-day delay. Lag observations alone cannot prove
an upper delivery bound; an unbounded or undocumented delivery tail remains
`DeliveryCompleteness` blocked even after API EOF. Later arriving entries
invalidate the prior candidate day; retain both captures and re-review.

The fallback supervisor supplies a sequence-complete independently durable launch/exit
journal plus a sealed day boundary. It must acknowledge the launch externally
before allowing the child to execute, and block launch if it cannot do so. A
seal includes the previous seal digest, first/last sequence, count, durable
commit acknowledgement and all still-running launch IDs; missing sequence,
missing seal, restart of the supervisor without recovery, instance loss or
missing outer VM/platform boundary blocks affected intervals. Only a verified
seal or an established equivalent platform completeness guarantee can set
`complete_through_us`; it cannot be the collector's wall time. Carry-in states
must reference the previous independently sealed source. No completion
watermark is inferred from startup logs, Monitoring counts or an empty query.

## Additional supervisor fallback and correlation

This is the fallback when the selected platform or existing launcher source cannot satisfy the qualification. A parent outside the evidence-writing process owns every process launch and wait/exit, including in-instance restarts. For the router it can wrap the single worker and forward termination within the existing grace; for the enclave first determine whether the existing Confidential Space launcher can supply the required records and joins. A new wrapper is required only for a demonstrated remaining gap. The fallback journal must durably record a launch externally before releasing the child and retain independently verifiable sequence/day boundaries. Local-only records cannot establish recovery after complete instance loss. Missing parent exit or outer instance boundary keeps the interval blocked.

Supervisor log projection `jsonPayload`:
`kind=a3_process_launch|a3_process_exit|a3_process_boundary`, `supervisor`, scoped
`instance`, unique `launch` (supervisor generation plus monotonic launch sequence),
`role`, full `revision`, true `region`, `at_us`, and for exit `event=stop|kill`.
Native log envelope retains `insertId` and original source metadata. Boundary
means independently sealed alive-through-day-end, not a process exit. The
journal/seal verification is a named native qualification seam; this round's
fake ports exercise its explicit input contract, not a production implementation.

Writer record `a3_startup_correlation` has `instance,launch,boot,role,revision,
region,process_start_us,recorded_at_us`. Boot is the actual journal boot UUID.
Router obtains platform instance ID using the existing metadata-server pattern
in `gcs_singleflight.py`, revision from `K_REVISION` plus released SHA; enclave
obtains the same data through its host launch descriptor and attested release.
Writer emits one bounded content-free record before readiness. It may only join
an independent launch by exact scoped instance/launch and matching revision/region,
with process start within its independently observed interval. Missing,
ambiguous or conflicting correlation blocks that launch. Correlation never
creates, deletes or counts an expected launch.

## Router ruling and midnight boundaries

The one-worker command and sampled maximum of one start/stop per instance support
a candidate, not the enforced one-process condition. The router witnesses are:

1. A process terminated before its first startup correlation still has an independently enumerated launch. Retain the original platform event, instance/revision/region, termination observation (or explicitly missing endpoint), the completely read startup channel, and zero correlations/rows. The collector must keep the unmatched launch and block its affected interval. Exercise child termination and whole-container loss; identify actual crash/OOM/kill message kinds, not just graceful shutdown.
2. A restart while preserving instance identity produces two distinct independent launches and, when emitted, two boot correlations and readiness populations. If the enforced Cloud Run model instead replaces the instance, retain both old/new instance starts and exits, the configuration that prevents another worker under the old ID, and results for permitted requested and crash restart paths. Any second process under the same ID invalidates instance-only launch identity. A sampled absence of repeats is insufficient.

The full-day sample has 28 starts without that day's stop and 8 stops whose starts
precede the day. Retain independent carry-in starts for the latter and an
independently established alive-through-day-end boundary or a later observed exit
for the former. The same rule applies across every midnight, including an
instance already alive at day start and still alive at day end. The Cloud Run
parser has no alive-through-end event: `RouterDayBoundary` remains a required
adapter/qualification input. Do not omit such instances or invent an exit.
The samples establish neither delivery completeness, retention, in-VM restarts,
nor unobserved crash/OOM/kill kinds for either candidate.

## Executable qualification procedures (not executed this round)

The activation package implements the isolated harness entry point described
here; these are acceptance commands, not claims of an existing executable.
Its `--capture` output is the finite capture plan consumed by this package.
Use an isolated copy of each named deployment configuration, with no opted
traffic or money enabled. The experiment operator must
approve the finite resource/day plan and injected failures.

1. `lifecycle-qualification --deployment router|enclave --case kill-before-startup
   --pause-child before-correlation --signal KILL --capture <new-dir>`.
   Pause the workload before its first correlation and terminate it. Determine
   whether the selected platform/launcher independently retained that launch,
   including loss before its start message. Fetch original platform and launcher
   pages (fallback journal/seal if selected) and the complete startup channel
   to EOF after independently established delivery closure. Expected: one launch, one kill/exit (or explicitly missing endpoint),
   zero correlations, zero writer rows; collector `expected` retains that launch
   with null boot and G2 blocked. Also kill the entire container/VM at the barrier:
   expect outer deletion/stop and a missing parent exit unless independently
   recovered; the result must remain blocked. This discovers crash/kill message
   types and tests loss before first flush, not just graceful SIGTERM.
2. `lifecycle-qualification --deployment router|enclave --case same-instance-restart
   --restart-child --capture <new-dir>`.
   Keep platform identity fixed; launch workload A, terminate it, restart workload B,
   and retain the selected source interval boundary. Expected: same instance, distinct independent launch
   IDs, two exits, distinct boot UUID correlations and two readiness populations.
   A forced crash and a requested restart are separate runs. If Cloud Run instead
   always replaces the instance, retain both instance starts/stops, configuration
   proving only one child/worker and failure-mode results; this establishes only
   the tested/enforced replacement policy. Any same-ID second process invalidates
   `cloud_run_instance` mode and requires a distinct independent launch source. Enclave VM reuse must show
   two independently recorded workload launches even if the audit log has only one VM insert/delete.
3. Capture bucket/sink/exclusion configuration, query the oldest required retained
   day to EOF, run delayed-delivery/parent-disconnection/seal-gap injections, and
   compare sequence totals plus outer lifecycle to captured pages. These establish
   retention and recovery/completeness mechanics; absent delivery bound/seal keeps
   `delivery_complete=false`. One-process-per-instance, crash/kill kinds, retention,
   delivery completeness and enclave in-VM restart coverage are **not established**
   by the supplied platform evidence or the unit tests.

No native qualification harness exists in C1. The activation package must run both witnesses and establish retention and delivery completeness for the selected source before opting ingress in. Use the existing platform or launcher when it meets those requirements; implement additional supervision only for a demonstrated missing requirement. The offline collector evaluates retained captures and does not establish qualification by itself.

## Retained observability channel and activation interface

Router records go to its existing structured stdout channel,
`projects/quill-cloud-proxy/logs/run.googleapis.com%2Fstdout`, with the existing
Cloud Run resource/instance labels. Enclave records go to redirected workload
stdout/stderr, `projects/quill-cloud-proxy/logs/confidential-space-launcher`.
The local reference `qcp-ref/tools/dx/enclave-logs.sh:3–10` identifies that exact
channel; `qcp-ref/tools/deploy-gcp-mig.sh:347,570` sets log redirect and
`tee-restart-policy=Always`. This is additional **repository configuration**
evidence, not a production observation. It reinforces why VM insert/delete alone
cannot establish enclave process completeness. Launcher records may wrap the
workload JSON in `jsonPayload.MESSAGE`; the captured parser must preserve the
original envelope and exact inner bytes, never regex-match a PASS string.

No frozen store kind fits startup correlation or component monotonic spans:
PREREQUISITE_CONTEXT lacks instance/launch identity; HEALTH_BASIS owns a specific
health read; COUNTER has aggregates; CLOSURE/CLOSE_RESULT describe transactions,
not the whole lifecycle. Thus these are content-free records in **existing log
channels**, not new evidence tables or modifications to any frozen schema.
HEALTH_BASIS, HEALTH_CLAIM, HEALTH and public descriptors are retained with their
existing validators and referenced original bytes. The capture adapter turns
exact log/artifact locators into bounded FILE_REFs; runtime does not know a
collector's filesystem path. All correlated data uses ordinary existing service
identity, without a new cross-boot record service.

The activation handoff's exact record definitions, timing/bounds and role checks
are in [the runtime interface](async-settle-shadow-v2-runtime-harness-interface.md).
Its named seams distinguish native qualification and runtime enforcement from
the offline parsers. The
collector command is executable now:

```sh
.venv/bin/python -m scripts.async_settle.shadow_v2_collect \
  --capture <captured-directory>/capture.json --out <new-output-directory>
```

It cannot launch the qualification experiment, enforce ingress or perform an API
call. Exit 2 retains review subjects and missing states. It never overwrites a
prior output. `capture.json` contains only finite FILE_REFs, call captures,
queries, the binding/pins and optionally supplied approval/previous-ledger/closure
references. A review seals the exact source digest in a **new** output directory;
it never changes the original measured trial or boot adoption times.
