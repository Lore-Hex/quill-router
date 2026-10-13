# Router startup harness, round 1

This round adds runtime records and an admission hold. It is incomplete and is
not ready for release. In the installed application, missing startup producers
keep opted handoffs unavailable. Ordinary traffic follows the existing path.
There are no contract, collector, exporter, fixture, pin or store-kind changes.

## Configuration and records

`TR_ASYNC_SETTLE_SHADOW_STARTUP_MANIFEST` names one reviewed local regular JSON
file, at most 262144 bytes, canonical UTF-8 (sorted keys, compact separators,
no trailing newline). No directory search occurs. Missing files leave the boot
missing. The closed top-level fields are:

```
launch revision region config_sha256 policy_sha256 fixture_sha256 config
 effective_pins public_descriptors artifacts selections ready_key_prefix
```

`launch` holds `mode=instance_as_launch`, `project`, `region`, `service`, and
`revisions` mapping K_REVISION names to full released source SHAs. Inherited
read-only fd 3 takes precedence and must contain exactly one canonical object
`{instance,launch,role,revision,region}` followed by EOF, within 2048 bytes and
250 ms. A present malformed descriptor never falls back. Instance mode obtains
only the instance ID from the fixed metadata endpoint, using the existing
Metadata-Flavor pattern and a one-second timeout. Both scoped IDs use SHA-256
of canonical `{source,coordinate}` as specified by the interface. Acquisition
mode and the original source coordinate stay on the Descriptor object; the
closed correlation record has no extra mode field.

`revision`, `region` and all three digests have the interface's shapes.
`config_sha256` must hash canonical `config`. `config.dependencies.router` is a
list of 2..16 unique `{operation,resource,region}` targets; operations include
both transport and read. Targets are passed only from this loaded configuration
to the dependency port. `config.descriptor_ids` is a bounded list of identifiers.
`effective_pins` has exactly the three router pins; deployed settings are compared
before checks. Booleans are emitted as their actual `true`/`false` values,
including OFF values. `public_descriptors` is the exact object containing
`authority_epoch,config_sha256,source_revision,descriptor_ids`.

`artifacts` maps at most 32 artifact IDs to objects with exactly:

```
id criterion source revision region config_sha256 started_at_us completed_at_us checks
```

`checks` has that criterion's exact check names and pass/fail values.
`selections` maps each criterion to its exact source-role-to-artifact-ID map.
The six criteria and source roles equal the collectors' inventory. Artifact
original times are unchanged; completion must follow start and precede adoption.
Non-CI trials span at most one hour. Artifact failures remain failures. A missing
source emits no completed substitute. The current manifest is a projection of
reviewed CI/trial results; loading and joining the original CI/trial documents,
enclave revision and trial configuration envelope remain ReleaseArtifactOriginals.
It must not be presented as a complete native release bundle.

Each criterion is attempted once. Adoption time is sampled locally after the
selected reads and measurements. Repeating run/adopt cannot replace a missing
or failed adoption. The observer port carries original buffers and install,
claim and publication times, with the 250000/500000 microsecond ceilings.
Its production adapter is unfinished. The coordinator requires a validator port;
tests supply the existing native HEALTH_BASIS and publication validators. No
observer adoption is possible without that validator port.

The runtime inventory contains all five closed kinds, role fields, coordinate
kinds/ceilings and enums. Tests compare it to the dormant collectors without
runtime imports of those collectors. A coordinate hashes and counts the same
buffer. The emitter drops invalid records and counts drops. Its default logger
writes compact JSON lines to stdout. This base tree's existing application
handler uses stderr and a text prefix, so it cannot supply the interface's exact
stdout records unchanged. A dedicated ordinary StreamHandler is used; no extra
record kind or descriptive payload is added.

## Listener and admission

Application construction attaches the hold to the compared Runtime.
`DrainingServer.startup` first awaits the existing listener startup, then schedules
the harness. It does not await manifest or platform work. The coordinator has
bounded file size, descriptor, dependency, observer and object-write steps.
Alternative ASGI launchers must call `listener_started` after their listener is
up; merely finishing application lifespan does not establish that boundary.

`Runtime.authorize_http` reads the startup flag once. Until ready, it uses the
existing `missing_context` unresolved receipt path, producing no opted handoff.
The existing cohort checks still decide which requests reach that runtime.
Non-cohort requests get the same response fields and no new records. Direct
unit-test Runtime construction retains its existing behavior unless a startup
hold is installed; application construction installs the hold.

Six passing records precede one ready timestamp, sampled strictly after all six
completions. A clock that has not advanced leaves readiness unavailable; no
invented microsecond is added. The validated ready original is emitted once,
then written once to the existing attachments bucket under
`ready_key_prefix + '/' + boot + '.json'`, with create-if-absent object semantics.
A thin adapter reuses GCSAttachments' bounded worker, Lifetime, request and reply
bounds while preserving the object metadata response that this base transport
otherwise discards. Name, length and positive generation are checked. The
coordinator retains key, generation, digest, length and original bytes and only
then opens the hold. Stdout is never an acknowledgement. A failed write or reply
never gets a second ready timestamp.

`PreIngressRetention` has a fake-tested write/acknowledgement implementation.
The release owner must still establish at least 30-day bucket retention and
independent capture of the original and generation. Other original buffers are
currently bounded in memory; their complete retained export is unfinished.

## Attempts and daily close

The P3 acknowledged-page callback passes actual PAGE rows to the recorder after
the existing acknowledgement. PAGE parsing remains with the existing row parser.
The record selects native authorize start events, their original start time,
sequence and day, and computes a coordinate from the exact PAGE body. It does not
use the later page-storage or emission time. Replayed events lacking a native
start locator remain missing. The recorder requires a separately supplied result for each sequence and accepts
all four result values. NativeAttemptResults must carry those actual results
and distinguish an eligible native event whose later handoff failed. Until that
seam is supplied, native attempts remain recorded by P3, while result projections
remain missing; the recorder does not infer accepted from eligibility.

The shutdown builder checks all five actual monotonic endpoint pairs, their
contiguity and nesting, and joins the descriptor and boot origin. Missing
endpoints produce no final span, including termination mid-pass. It never fills
missing endpoints with zeros or attributes an unobserved hard kill to this
process. Repeated day closes emit at most once. The builder is tested with
simulated endpoint observations, not a live P3 lifecycle measurement.

P3ShutdownEndpoints is unfinished: this tree exposes close/result transaction
boundaries, but not the preparation/export-wait/final-pass endpoint set needed
for both midnight and termination. No wrappers invent those intervals. P3's
SIGTERM behavior and timing logic are untouched. NativeExport closure/result
original retention and the remaining-grace comparison also need binding to this
recorder. There is no claim that daily or terminating spans are emitted live.

## Stage and seam ledger

* **RouterLaunchDescriptor qualification:** acquisition exists; bridge source
  retention and its exact independent launch join remain release work.
* **Two witnesses:** termination before correlation, and restart with preserved
  instance identity (or enforced replacement), remain release work. This package
  neither runs them nor establishes one process per instance.
* **BootClockQualification:** the boot UTC original and monotonic origin exist;
  native clock relation, adjustment and uncertainty qualification remain release
  work. The current process-start marker is sampled when this module loads,
  not an independently obtained operating-system creation timestamp.
* **ReleasedDependencyOperations:** the bounded target-driven port exists; no
  production regional-operation registry is installed. Local transport remains
  missing. A resource string must be bound to the actual configured client,
  marker and deployed pin before that port can report a completed observation.
* **ObserverInstallOriginals:** no production hook retains HEALTH_BASIS at the
  actual warm install, nor the exact contender claim/publication write buffers.
  The coordinator requires the existing native validators through a supplied
  callable as well as its projection joins. The production adapter must still
  connect those validators and producer buffers.
* **ReleaseArtifactOriginals:** original CI/trial/release loading, not just their
  manifest projections, needs completion. Public descriptors now use the exact
  object slice of the retained canonical manifest.
* **PreIngressRetention / StartupOriginalRetention:** ready write/acknowledgement
  is implemented; native retention qualification and export of all other
  originals and mode/source coordinates remain unfinished.
* **NativeAttemptResults:** exact accepted/failed/rejected/timeout result joins,
  replay/unavailable-page handling and complete denominator retention remain
  unfinished as described above.
* **P3ShutdownEndpoints:** actual daily and terminating phase observations and
  native close/result original joins remain unfinished.
* **JournalBootAllocation:** when no P3 service exists, construction records this
  missing seam in app state. It does not allocate a replacement P3 boot. Thus
  the requested idle/no-cohort record-producing runtime is not yet implemented.
* **ListenerStarted:** the packaged serving entry point is wired; alternative
  hosts need their own post-listener callback.
* **EnclaveOptedAdmission:** the enclave owner must identify and hold its actual
  opted admission callable until its own six checks and retained ready
  acknowledgement. This router package does not implement that callable.

Missing runtime ports keep production readiness unavailable. This is an
incomplete round-one implementation, not an activation or release claim.

## Interface sentences not yet implemented as written

The interface document is unchanged. The smallest proposed change for each item
below is implementation work at the named seam, not a weaker interface rule.

> The producer computes both digest and byte count from the same retained buffer, never from request fields, filenames, a later reconstruction, or a declared result.

Public descriptor coordinates now use the exact object slice of the loaded
manifest buffer. The production observer claim/publication buffers still need
their actual producer retention hook; they cannot be reconstructed later.

> Parse HEALTH_BASIS with its existing validator, require reader_boot and installed source state.

ObserverInstallOriginals must expose exact P3 producer buffers and the install
endpoint. The required validator port is tested with the existing native
validators; it has no production adapter yet.

> A contender also retains parsed HEALTH_CLAIM and HEALTH, validates their publication join, owner boot, actual claim and publication clocks within this boot and before installation, with publication <=500000 us.

The contender port needs the native validators and actual producer write buffers.

> Retain the ready original and acknowledgement for >=30 days.

The object write is implemented; independent retention and acknowledgement
capture configuration must be supplied before use.

> The recorder writes the single result projection when the result is known and the page containing that start event has been retained; accepted means admission accepted, not eventual settlement.

NativeAttemptResults must carry the actual result alongside the existing event
start boundary, including failures after native event finalization.

> The close coordinator writes one final a3_shutdown_span per inventoried router writer/day interval after the measured final pass has ended.

Add P3ShutdownEndpoints at actual lifecycle boundaries, without changing their
budgets or sequencing. The current standalone builder is not that integration.

> Write the correlation once, immediately after reading the descriptor and allocating the journal boot, before boot checks and before any ready record.

The inherited descriptor is read and correlated before loading the manifest,
but the existing journal was allocated earlier during application construction.
Move descriptor delivery and correlation to the allocation boundary while
retaining post-listener checks; add the no-cohort journal owner.

> process_start_us is the measured process start, recorded_at_us is this correlation write time.

Replace the module-load marker with a qualified entry-point/process-start
measurement supplied by RouterLaunchDescriptor.

The requested design section 15 is absent from this checkout's design document
(the file ends with section 14.3). The interface's six-criterion table was used.
The named observation-service glob is also absent; its implementation is
`services/async_settle_shadow_health.py`. The requested generation receipt is
absent from the base attachments API; the ready-only adapter preserves it.
