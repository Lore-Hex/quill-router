# A3-C1 process inventory source binding, version 1

Status: source protocol submitted for owner/release review in round 2. This is
not a claim that a reviewer approved these bytes or that native qualification
ran. The only production observations used are the names/counts in
`/tmp/v2-live-author-2c1/platform-evidence.md`, dated 2026-10-13. No network or
production read occurred in this implementation round. The approved A3 design,
§15.3 and §13 Q6, controls. `ProcessCompletenessQualification` remains CLOSED
for production until the witnesses and delivery boundary below are captured.
A flag in a capture is not a new source of authority: the independently reviewed
deployment FILE_REF and release-plan digest must bind the originals and adapter.

## Decision and independent population

Bind the router to the platform-owned Cloud Run system instance lifecycle log.
Bind the enclave VM population to Compute Admin Activity insert/delete events
and captured membership history of the four explicitly named MIGs. Neither
current revision listing nor surviving writer startup messages enumerates
processes. These platform sources are sufficient for *process* inventory only
if qualification establishes and deployment enforces one evidence process per
observed instance lifetime. That condition is currently unproved for Cloud Run
and false as a general assumption for a VM. Use the external supervisor protocol
below whenever an in-instance restart is possible. Keep the platform lifetime
as the containing independent VM/container lifetime in that protocol.

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

## Enclave adapter: compute_instance plus external process supervisor

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

VM identity is `(project,zone,numeric instance_id)`, never a reusable VM name.
`binding.instances[instance_id]` supplies the reviewed exact `resource_name,mig,
revision,region` join. In `compute_instance` mode the launch ID is that scoped VM
identity, conditional on one process per lifetime. An insert completion does
not tell when an enclave process started; writer correlation supplies process
start, constrained to the containing independent interval. Delete completion
is a VM endpoint, not a graceful enclave close. It does not distinguish an
in-VM process restart or kill. A3 first-window activation therefore requires
the supervisor unless a separately reviewed deployment prohibits and proves
all in-VM restarts. Audit events remain required independent corroboration.

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

The supervisor supplies a sequence-complete independently durable launch/exit
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

## Smallest external supervisor and correlation

One parent supervisor outside the evidence-writing process owns process launch
and wait/exit. Router: container PID 1 parent wraps the existing one worker and
forwards SIGTERM, retaining the existing grace; no detached in-container second
writer. Enclave: host service manager wrapper outside the enclave writer owns
every launch, including restarts inside the same VM. Both require a durable
external append acknowledgement before `exec`/release of the child. A local-only
log or `ExecStartPost` is insufficient for a kill before startup. This does not
need a new evidence table, signing service or per-boot key: use a retained
observability channel with ordinary service identity and independently reviewed
retention/sequence sealing. Complete parent/VM loss without a recoverable exit
remains blocked; a finally block cannot certify arbitrary kills.

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

## Executable qualification procedures (not executed this round)

The activation package implements the isolated harness entry point described
here; these are acceptance commands, not claims of an existing executable.
Its `--capture` output is the finite capture plan consumed by this package.
Use an isolated copy of each named deployment configuration, with no opted
traffic, production credentials or money enabled. The experiment operator must
approve the finite resource/day plan and injected failures.

1. `lifecycle-qualification --deployment router|enclave --case kill-before-startup
   --pause-child before-correlation --signal KILL --capture <new-dir>`.
   Parent durably records launch, releases child to the pre-correlation barrier,
   harness kills child, parent records kill/exit. Fetch original platform pages,
   parent journal/seal and the complete startup channel to EOF after delivery
   closure. Expected: one launch, one kill/exit (or explicitly missing endpoint),
   zero correlations, zero writer rows; collector `expected` retains that launch
   with null boot and G2 blocked. Also kill the entire container/VM at the barrier:
   expect outer deletion/stop and a missing parent exit unless independently
   recovered; the result must remain blocked. This discovers crash/kill message
   types and tests loss before first flush, not just graceful SIGTERM.
2. `lifecycle-qualification --deployment router|enclave --case same-instance-restart
   --restart-child --capture <new-dir>`.
   Keep platform identity fixed; launch child A, terminate it, restart child B,
   and seal the interval. Expected: same instance, distinct independent launch
   IDs, two exits, distinct boot UUID correlations and two readiness populations.
   A forced crash and a requested restart are separate runs. If Cloud Run instead
   always replaces the instance, retain both instance starts/stops, configuration
   proving only one child/worker and failure-mode results; this establishes only
   the tested/enforced replacement policy. Any same-ID second process invalidates
   `cloud_run_instance` mode and requires the parent. Enclave VM reuse must show
   two supervisor launches even if the audit log has only one VM insert/delete.
3. Capture bucket/sink/exclusion configuration, query the oldest required retained
   day to EOF, run delayed-delivery/parent-disconnection/seal-gap injections, and
   compare sequence totals plus outer lifecycle to captured pages. These establish
   retention and recovery/completeness mechanics; absent delivery bound/seal keeps
   `delivery_complete=false`. One-process-per-instance, crash/kill kinds, retention,
   delivery completeness and enclave in-VM restart coverage are **not established**
   by the supplied platform evidence or the unit tests.

No native harness exists in C1. These exact experiments and the supervisor are
activation work under `ProcessCompletenessQualification`/`StartupHarness`; the
offline collector is available to evaluate their captures. Review and implement
that instrumentation before opting ingress in, not after a candidate window.

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
identity, not a new cross-boot signing authority.

The activation handoff's exact record definitions, timing/bounds and role checks
are in `/tmp/v2-live-author-2c1/report.md`. That report also distinguishes the
unimplemented native and runtime seams from the completed offline parsers. The
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
