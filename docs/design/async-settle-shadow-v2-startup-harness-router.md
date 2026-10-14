# Router startup records — round 5

The package runs after the serving listener starts. Application construction
installs the admission hold and allocates the boot; it does not wait for files,
store operations or object writes. Non-cohort behavior and the capture path are
unchanged. The closed record inventory and existing native parsers are unchanged.

## Connected producers

P3's HealthObserver retains its first usable HEALTH_BASIS buffer at the actual
install. The buffer is produced and parsed there, with reader boot and install
clock; the startup reader never reconstructs it from a later cache snapshot.
The observation store retains the exact Row.body buffers submitted for the
successful HEALTH_CLAIM and HEALTH writes. The publisher's owner reply carries
these bytes and its measured cycle time to the service. The published interval
is the difference between the retained publication and claim timestamps; the
whole producer cycle has its own bound. A contender adoption
joins these originals through the native publication and HEALTH_BASIS parsers.
The reader boot must match, source state must be installed, installation must
be within 250000 us, and publication within 500000 us. Both publication clocks
must be in this boot and precede installation. A missing join stays missing.

With a service, startup uses service.journal.boot. Without one, it imports and
calls uuid.uuid4, the exact allocation function used by P3's Counters. Idle app
construction now installs the coordinator instead of returning without a boot.
A configured launch descriptor can therefore produce an idle correlation and
the available adoptions. The marker adoption compares the real settings with
the declared pins, including the dormant OFF values.

The regional-operation adapter binds read to the configured P3 database ID and
its existing ObservationStore. It performs one HEALTH local/fleet exact-key
read, with one attempt and a bounded budget. Target membership, region and
configured database are checked. Operations run off the listener's task; the
criterion's outer deadline remains at most ten seconds. There is no retry.
The transport operation reads the regional enclave resource described below.
Without P3, the read uses the configured database through the same ObservationStore
adapter; it does not start an observer or publisher.

Runtime.authorize_http reads the admission latch at its existing native start
boundary. Its already-owned producer input carries the reservation, event and
returned result through the existing bounded deferred queue. The queue carries
the already-computed capability boolean. The final handoff
finishes before transfer, so a handoff failure carries its actual result. No
NativeAttempt, attempt deque, request counter or result closure remains on the
request path. The worker consumes each native input once and records its start
and result there, including an input without a journal reservation.
Accepted means the admission handoff was produced. It does not mean settlement
completed. Rejected, failed and timeout results use the same start coordinate.
The PAGE callback runs after P3 acknowledgement and joins the actual result to
the native start event and exact PAGE bytes. Result and PAGE may arrive in either
order. Missing results, missing pages and replay events without a start locator
have no projection. Pending joins are bounded at 8192; overflow is counted.
Recent emitted joins are bounded separately. A busy recorder counts a missing
join and leaves native admission unchanged; it never waits on its join lock. Native PAGE retention remains P3's
job; the harness does not change capture or the sample writer.

The P3 close generator now supplies an optional observation callback. It samples
preparation, attachment wait, final pass, close and result endpoints in the
service's monotonic clock. Termination preparation starts at close entry; daily
preparation starts in the closing flush. The first preparation/upload boundary
is kept across the preliminary and final passes. Close and result intervals
come from the actual calls, including failed calls. The callback receives the
exact closure and close-result Row.body originals. It adds no wait, retry or
store operation to P3. A bounded result attempt can produce complete=false;
an unobserved result phase produces no span. A process cannot report its own
unobserved end. There is one final record per day, joined to the boot origin.
This checkout did not contain the five named trace events described in the
round-2 request; these endpoints are observed directly in the existing generator.

## Original loading and retention

The canonical startup manifest names each artifact with a configured path and
its declared joins. Each original has exactly id, criterion, source,
router_revision, enclave_revision, region, config_sha256, started_at_us,
completed_at_us, checks and conclusion. The configured enclave revision is
config.enclave_revision. Reads are bounded to 65536 bytes per original and a
finite manifest inventory. Times, checks and revisions are parsed from the
original file bytes and compared with the declaration. Missing files and
mismatched joins never become passing adoptions. Public descriptors retain the
exact object slice from the canonical manifest.

Startup originals use the existing attachments transport with create-if-absent
writes. Every reply must contain the matching key, digest, length and positive
generation before readiness opens. Under ready_key_prefix/boot/ the key plan is:

- descriptor.json, mode.json, and source.json when the descriptor supplies it;
- correlation.json, manifest.json, and adoptions/criterion.json;
- artifacts/sha256.json for the exact CI, trial and release originals;
- originals/kind/sha256.json for emitted original coordinates;
- index.json, containing the keys, digests, lengths and returned generations.

The ready original keeps the round-1 key ready_key_prefix/boot.json. It is written
after the startup originals and their index are acknowledged. Available records
and originals are retained even when an adoption is missing. PAGE, closure and
close-result rows keep their existing P3 keys and retention path. The release
must configure the required retention duration and capture the generation
replies; a local stdout record is not an object acknowledgement.

## Regional transport operation

The router does not make an outbound settle-echo call. The echo arrives on the
enclave's calls to the router. Its regional transport dependency is the enclave
endpoint itself. `dependencies.router` supplies the exact released resource
and the router region. `regional_attestation_resource` matches that resource
against `ENCLAVE_REGIONS` and `synthetic.probes.configured_targets(Settings)`.
The two region inventories are independent. The startup module does not import
the cohort probe or construct a hostname from the router region.

| Deployed router region | Released dependency mapping |
| --- | --- |
| us-central1 | Explicit configured enclave endpoint; normally us-central1 |
| us-east4 | Explicit configured enclave endpoint; normally us-east4 |
| europe-west4 | Explicit configured enclave endpoint; normally europe-west4 |
| southamerica-east1 | Explicit remote enclave endpoint; the tested release mapping uses us-central1 |

There is no implicit São Paulo fallback or invented local endpoint. A release
can select a different configured enclave endpoint through the same inputs.
A missing or unlisted resource remains missing. The target's region field still
names this router's region, including southamerica-east1. No request field
supplies the resource or region.

Target resolution, imports, client construction, context loading and the single
GET all run in the owned startup worker with its own event loop. The GET has
an inner deadline leaving up to 100 ms of its outer budget for stream and loop
closing. A still-running call keeps its slot and subsequent startup work stays
missing; there is never a second simultaneous startup-input worker.

The operation makes one read-only GET, with no retry or redirect, a 65536-byte
response limit and an absolute deadline of at most 10000000 us. It uses the
existing `receipt_keys.gcp_attestation_image_digest` reader and compares the
single image digest with the same loaded accepted-image set used by the router:
`StageDPolicyResolver.accepted_image_digests()` plus
`Settings.stage_d_accepted_gcp_digests`. It does not fetch a second pin source.
A matching, well-formed response completes with attempts=1 and
marker_matches=true. Missing pins, an unknown digest, a malformed response or
a timeout leaves boot_transport missing. The listener continues serving.
This is the startup image-marker comparison, not a new settle client.

The interface sentence implemented is: “The independent released configuration
has `dependencies.router` and `dependencies.enclave`, each a list of
`{operation,resource,region}`.” Collectors must include this exact regional
endpoint resource in `dependencies.router` with operation `transport` and
this boot's region, alongside the configured database `read` resource.


## Launch input ownership

Ordinary package initialization records only whether fd 3 was present, using
fstat. It never duplicates or reads that descriptor. The serving module entry
point calls launch_descriptor_state.claim before importing serving libraries.
An embedded launcher that explicitly delivers this input calls that same bridge
before its runtime imports and pairs claim with close in its serving finally
block. Merely importing an app does not claim its fd 3. Ownership transfers without duplication. read_fd closes the owned input on both
valid and invalid reads, and the serving exit closes an unread input. Explicit
read_fd(fd) calls borrow the caller's descriptor and leave its closing to that
caller. Processes that never claim a launch input keep their original descriptor
lifetime and offset, whether or not they construct a harness.

## Idle observer rule

`observer_freshness` requires this boot's own installed HEALTH_BASIS original.
That original exists only when the P3 durable-observation service runs in this
process. In the dormant stage, with no cohort configured, P3 need not run.
Given the released startup inputs, that boot records a3_startup_correlation and
five adoptions: transport, size and regional_load from the manifest,
boot_transport from the regional operations, and marker_keys with the actual
OFF pins. observer_freshness remains MISSING with the fixed reason
`no observer ran in this process`. There is no observer record and no ready
record. This is an ordinary dormant boot, with nothing opted.

At activation, P3 runs on every router. Its own HEALTH_BASIS install supplies
the sixth adoption. There is no synthetic idle record or replacement producer.

## P3 retention hooks

The store retains the already-built claim and publication Row.body buffers only
when a startup consumer is installed and the existing write completed. It adds
no store operation. The health-state helper only parses those originals and
checks their existing publication join. The service retains the first usable
install-time HEALTH_BASIS and carries the existing publisher reply buffers and
measured cycle duration. These record steps are conditional on the startup
consumer. The optional close callback retains existing close/result buffers;
its extra clock samples also run only with a consumer. Existing P3 deadlines,
scheduling, writes and absent-consumer behavior remain unchanged.

## Stage and seam ledger

- **Two witnesses:** an independent termination-before-correlation observation
  and a restart with preserved instance identity (or enforced replacement)
  remain release work. They block the launch population and restart joins.
- **RouterLaunchDescriptor qualification:** descriptor acquisition and mode/source
  retention exist. The release still needs the independent launcher mapping,
  source original and qualified process-start timestamp. These block acceptance
  of the correlation's launch/time join. The current process-start marker is
  sampled when the module loads.
- **BootClockQualification:** the boot UTC original and monotonic origin exist.
  The release must establish their relation, adjustments and uncertainty.
  This blocks adoption-time and elapsed-time qualification across clocks.
- **EnclaveOptedAdmission:** the enclave owner must connect its actual opted
  admission callable to its six checks and retained ready acknowledgement.
  This blocks activation of the complete compared route.

- **Retention duration and capture configuration:** release setup must retain ready
  originals and acknowledgements for at least 30 days and capture the complete
  startup originals and generation replies.

## Validation

The package test modules use the real P3 service and compared Runtime with
fakes at the store and transport boundaries. They cover original buffer identity,
actual outcomes and PAGE acknowledgement, incomplete starts and closes, original
file joins, generation replies, target selection and listener availability.
The round-5 report at /tmp/v2-harness-author-5/report.md records the requested
module runs, fresh-process import and complete authorize costs, and changed-copy
checks. Runtime HTTP tests use a real
HTTP handler over connected sockets, because this runner cannot bind loopback
ports; HTTPX parsing, response reads and deadlines remain in the tested path.
Native capture costs are reported separately from complete authorize costs.
