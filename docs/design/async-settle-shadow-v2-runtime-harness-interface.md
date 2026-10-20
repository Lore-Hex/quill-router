# A3-C1 runtime harness interface, proposal version 2

Status: under review, offline and dormant. This document does not approve a source,
qualify native capture, enable ingress or activate a release. The source-binding
proposal is [process inventory](async-settle-shadow-v2-process-inventory.md).
The runtime and native adapters remain implementation work for their owners.

## Record envelope and identities

The runtime emits exactly five projected record kinds below. Router structured
stdout uses `projects/quill-cloud-proxy/logs/run.googleapis.com%2Fstdout` and the
Cloud Run instance/revision/resource labels. Enclave workload stdout is carried
in MESSAGE in `projects/quill-cloud-proxy/logs/confidential-space-launcher` with
`gce_instance` labels. Capture preserves original response/envelope and inner
bytes separately, and distinguishes launcher-origin records as specified by
`LauncherOriginParser`. Workload stdout never enumerates launches.

All object keys are exact; omitted or extra keys are malformed. Canonical UTF-8
JSON bytes are limited to 2, 16, 1, 2 and 4 KiB respectively, before derived
packaging. Original captured pages remain retained even for malformed records.
A selected channel's complete kind inventory is classified before collecting;
no extra runtime kinds are currently reviewed. Every unclassified kind adds a
missing classification state. A future non-lifecycle kind needs an explicit
reviewed parser and inventory update; it cannot be silently ignored.

`boot,instance,launch,region`, source-role IDs and resource IDs are ASCII strings
matching `[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}`. Boot is the runtime journal's boot ID,
not a population source. Scoped instance and launch identities are joined through
the independently retained launch descriptor. Full released source `revision`
is a 40-character lowercase hexadecimal SHA. Cloud Run `revision_name` and
K_REVISION are platform resource names, retained in platform originals and mapped
through `binding.revisions`; they are never substituted for a released SHA.
Digests are exactly 64 lowercase hexadecimal characters. Integer coordinates
are JSON integers (not booleans), from 0 through 9223372036854775807. Role is
`router|enclave`; dates are ISO UTC days. Result/check values are fixed enums.

A captured source reference has exactly `path,sha256,bytes` under FILE_REF;
path is at most 256 characters, bytes at most 262144, with exact digest/length
and local path validation. Runtime uses bounded original coordinates; the
capture adapter resolves them to these references without changing source bytes.
The emitted form uses the same closed record keys below, replacing only the
listed reference fields with the closed coordinates in the next section. Both
emitted and captured forms must meet the published per-record byte limits.
No arbitrary text, attached objects or extra result descriptions are allowed.

## Emitted coordinates and retained-file joins

Every reference-bearing runtime field has exactly `{kind,sha256,bytes}` in the
emitted form. The machine inventory's `emitted_coordinates` lists every field,
its one allowed kind, byte ceiling and internally owned producer. `kind` is that
literal ASCII string; `sha256` is exactly 64 lowercase hexadecimal characters;
`bytes` is a JSON integer, not boolean, in 1..max_bytes. The digest is SHA-256 of
the exact original bytes, before any capture envelope. The producer computes
both digest and byte count from the same retained buffer, never from request
fields, filenames, a later reconstruction, or a declared result. No optional
coordinate keys or additional original kinds are allowed.

| Field | Exact original kind and internally owned value producer | Original-record join after byte resolution |
|---|---|---|
| clock_basis.original | boot_clock_recorder retains the exact JSON object `{clock,boot,boot_at_us}` when it records the boot's UTC relation | clock is utc_ns; boot and boot_at_us equal the enclosing clock_basis and inventoried boot |
| health_basis | health_reader retains its A1 HEALTH_BASIS buffer after the actual read and installation decision | reader_boot equals this boot; source_state is installed; read_at_ns/read_elapsed_ns join the measured install interval |
| claim | health_publisher retains its A1 HEALTH_CLAIM buffer from this contender's completed claim | owner_boot_id, slot and claim_at_ns join the publication and this boot's observation |
| publication | health_publisher retains the A1 HEALTH buffer it wrote | existing validate_publication joins claim; observed_at_ns joins published_at_ns; body equals installed HEALTH_BASIS.body |
| public_descriptors | release_loader retains the release-supplied exact JSON object `{authority_epoch,config_sha256,source_revision,descriptor_ids}` as loaded | positive epoch, config digest, released revision and ordered descriptor IDs equal this boot's released inputs |
| attested_image | image_record_loader retains the enclave-supplied exact JSON object `{revision,image_sha256}` as loaded | revision and image digest equal the released revision and config's enclave_image_sha256 |
| receipt.original | receipt_page_writer retains the existing v2 PAGE buffer containing the selected start event once that page is closed | PAGE boot/day, selected seq and event_at_us join receipt boot/day/sequence and attempt at_us as specified below |
| originals.original_close | daily_close_writer retains the A1 CLOSURE buffer written for this writer/day | boot/day/revision/region/interval_end_us join the inventory interval |
| originals.close_result | close_result_writer retains the A1 CLOSE_RESULT buffer after the actual close result | existing validate_close_result joins closure_sha256, boot/day/revision/final_checkpoint_seq; elapsed_us joins the measured close interval |

`originals.original_close` and `originals.close_result` are fields of the separate
NativeExport input, not extra keys on a3_shutdown_span. NativeExport groups these
coordinates by the exact `day + "/" + boot` key; the capture adapter supplies the
shutdown collector's existing `originals` map with resolved FILE_REF values.
An unavailable close or result is an absent map entry, never a coordinate for an
empty or invented original. All other rows replace the identically named field
inside the corresponding record; claim/publication occur only for a contender.
`a3_startup_correlation` and `a3_traffic_ready` have no FILE_REF fields.

The capture owner builds a finite original index for the named resource/day,
keyed by `(kind,sha256,bytes)` from retained source buffers of that kind. Each key
maps to one captured FILE_REF. Repeated identical bytes coalesce; choose the
lexicographically smallest local path if capture retained several copies.
Resolve by exact key lookup, then check FILE_REF length and digest against the
coordinate and read the bytes. Never search by boot alone, nearest timestamp,
or latest record. The offline `resolve_emitted_reference` helper performs this
bounded index lookup and byte check; collecting modules apply the original
validators and joins in the table. Missing index entries, changed files,
wrong kinds, or failed original joins retain a missing state. The adapter must
retain the emitted record and original source pages even when resolution fails;
it must not silently omit the record from the channel or attempt denominator.
The helper and declarations define the offline interface; acquiring native
originals and building their index remain adapter work.

For non-FILE_REF source selectors, boot checks' `inputs` values are exact bounded
artifact IDs supplied by the release loader's CI/trial manifest. They join one
retained artifact by id and then its source/criterion/release/config/region and
original times; they are not runtime-created filenames. Other pin digests and
revision fields come from that same internally loaded release bundle. Boot is
allocated by the local journal, times by the named local recorder, check results
by the criterion runner, and role/region/instance/launch by the descriptor bridge.
These fields are never copied from an incoming request.

## Launch descriptor acquisition and clocks

Router acquisition (`RouterLaunchDescriptor`, not implemented here) reads the
platform instance ID via the existing metadata-server pattern, K_REVISION and
the retained release mapping. Instance-as-launch is usable only after the
one-process-per-instance condition and both witnesses are proved and enforced.
In fallback supervisor mode the parent delivers its independently retained
launch descriptor before releasing the child. It is not created by the writer.

Launcher acquisition (`LauncherLaunchDescriptor`, not implemented here) supplies
the retained `(project,zone,instance_id,_BOOT_ID,start insertId)` identity or a
qualified launcher sequence with an exact one-to-one join to that start. Several
starts in one VM boot require separate identities. The actual acquisition method
and exact origin fields are missing from the names/counts samples. The native
owner must capture them and both witnesses; if the existing launcher cannot
supply them, implement the additional supervisor for that demonstrated gap.

The runtime delivery interface is one inherited read-only descriptor at file
descriptor 3, containing one canonical UTF-8 JSON object, at most 2048 bytes,
followed by EOF. It has exactly `instance,launch,role,revision,region` as declared
by `launch_descriptor_fields`: instance/launch/region use the 1..128 ASCII
identifier rule, role is router|enclave and revision is the released 40-character
SHA. The internally owned launch-descriptor bridge supplies every value before
runtime startup; the child reads it once, requires EOF within the byte bound,
and cannot proceed to ready with a missing or malformed descriptor. No environment
text or user request can replace the descriptor. The actual bridge installation
and native acquisition remain deferred.

The bridge retains its source coordinate separately using exactly the fields in
`launch_coordinate_fields`. Each value is a 1..128 ASCII identifier, except
child_start_sequence, which is a positive integer <=9223372036854775807.
Cloud Run project/location/service/revision_name/instance_id come from the
platform descriptor and retained revision mapping. Launcher project/zone/
instance_id/vm_boot_id/start_insert_id come from the original launch record
(vm_boot_id is its _BOOT_ID). Supervisor project/location/instance_id come from
its platform descriptor; supervisor_boot_id and child_start_sequence come from
its retained child-start record, allocated before child execution. No sequence
is inferred from the child's stdout. The native bridge must deliver the exact
retained start coordinate, rather than discovering it by a later time search.

Encode a scoped instance as `instance:` plus the SHA-256 hex digest of canonical
JSON `{source,coordinate}`. Here source is the corresponding key from
launch_coordinate_fields; coordinate is the full Cloud Run coordinate, the
launcher subset `{project,zone,instance_id}`, or the supervisor subset
`{project,location,instance_id}`. Encode launch as `launch:` plus the same digest
construction using the full source coordinate for every source. The independent
inventory adapter uses the identical encoding. The bridge copies role/region
from the retained deployment mapping and revision from the released SHA mapping.
Its retained mapping from descriptor to source coordinate must join exactly one
independent launch. This defines the encoding and delivery contract; the named
native witnesses, origin fields and bridge implementation remain outstanding.

`process_start_us,recorded_at_us,boot_at_us,completed_at_us,at_us,observed_at_us`
are UTC Unix microseconds; health source clocks ending `_ns` are UTC Unix
nanoseconds. Original trial start/completion remains the original source time;
boot adoption is `completed_at_us` in the boot check and context. The collector
requires typed original start < completion <= adoption for every selected
artifact, and <= one hour for native trials. A shared trial retains identical
original times when adopted by multiple later boots.

Local observations additionally retain `clock_basis` exactly
`{clock:"utc_ns",boot,boot_at_us,original}`. Its original is a captured clock
qualification record exactly `{clock,boot,boot_at_us}` with the same values.
The native capture/review owner must establish that these clocks describe this
process and their UTC relationship, including any clock adjustment or uncertainty;
a projection declaring that relationship is not native qualification. Absent
qualification blocks release (`BootClockQualification`). Comparisons below use
that relationship and never subtract an unrelated wall and monotonic clock.

## Closed runtime schemas

This machine-readable inventory is tested against the collector constants.
Field strings list exact keys, not optional suggestions. Role fields are added
only for that criterion and role. For a router observer with `contender=true`,
add exactly `claim,publication,publication_elapsed_us,claim_at_ns,published_at_ns`.
Otherwise those fields are absent.

```json
{
  "fields": {
    "a3_startup_correlation": "kind instance launch boot role revision region process_start_us recorded_at_us",
    "a3_boot_check": "kind boot role revision region boot_at_us criterion completed_at_us checks config_sha256 inputs",
    "a3_traffic_ready": "kind boot at_us",
    "a3_opted_attempt": "kind boot at_us result receipt",
    "a3_shutdown_span": "kind boot instance launch revision region day interval_end_us complete clock clock_origin spans outcome"
  },
  "role_fields": {
    "boot_transport": {
      "router": "dependencies clock_basis",
      "enclave": "dependencies clock_basis"
    },
    "observer_freshness": {
      "router": "health_basis install_elapsed_us contender installed_at_ns clock_basis",
      "enclave": "router_path configured_router_region local_transport_ready echo_ready clock_basis"
    },
    "marker_keys": {
      "router": "effective_pins source_revision authority_epoch public_descriptors policy_sha256 fixture_sha256",
      "enclave": "effective_pins source_revision authority_epoch public_descriptors policy_sha256 fixture_sha256 attested_image attested_revision"
    }
  },
  "limits": {
    "a3_startup_correlation": 2048,
    "a3_boot_check": 16384,
    "a3_traffic_ready": 1024,
    "a3_opted_attempt": 2048,
    "a3_shutdown_span": 4096
  },
  "checks": {
    "transport": [
      "actual_hops",
      "both_routes_variants",
      "compared_outcome"
    ],
    "size": [
      "event_snapshot",
      "headers_fragments",
      "native_memory",
      "transactions"
    ],
    "regional_load": [
      "cpu_reads",
      "envelope",
      "publisher_export_shutdown"
    ],
    "boot_transport": [
      "local_dependencies",
      "regional_hops"
    ],
    "observer_freshness": [
      "role_warm_path",
      "slot_freshness"
    ],
    "marker_keys": [
      "deployed_pins",
      "public_epoch",
      "separation_negatives"
    ]
  },
  "sources": {
    "transport": [
      "ci",
      "regional_trial"
    ],
    "size": [
      "ci",
      "sizing_trial"
    ],
    "regional_load": [
      "budget_trial"
    ],
    "boot_transport": [
      "regional_trial"
    ],
    "observer_freshness": [],
    "marker_keys": [
      "ci"
    ]
  },
  "attempt_results": [
    "accepted",
    "failed",
    "rejected",
    "timeout"
  ],
  "channels": {
    "startup": [
      "a3_startup_correlation"
    ],
    "readiness": [
      "a3_boot_check",
      "a3_traffic_ready",
      "a3_opted_attempt"
    ],
    "shutdown": [
      "a3_shutdown_span"
    ]
  },
  "emitted_coordinate_fields": "kind sha256 bytes",
  "emitted_coordinate_types": {
    "kind": "field_kind",
    "sha256": "lower_hex_64",
    "bytes": "positive_integer_to_max_bytes"
  },
  "emitted_coordinates": {
    "clock_basis.original": {
      "kind": "boot_clock",
      "max_bytes": 262144,
      "producer": "boot_clock_recorder"
    },
    "health_basis": {
      "kind": "health_basis",
      "max_bytes": 6144,
      "producer": "health_reader"
    },
    "claim": {
      "kind": "health_claim",
      "max_bytes": 1024,
      "producer": "health_publisher"
    },
    "publication": {
      "kind": "health",
      "max_bytes": 4096,
      "producer": "health_publisher"
    },
    "public_descriptors": {
      "kind": "public_descriptors",
      "max_bytes": 16384,
      "producer": "release_loader"
    },
    "attested_image": {
      "kind": "image_record",
      "max_bytes": 16384,
      "producer": "image_record_loader"
    },
    "receipt.original": {
      "kind": "receipt_page",
      "max_bytes": 65536,
      "producer": "receipt_page_writer"
    },
    "originals.original_close": {
      "kind": "closure",
      "max_bytes": 4096,
      "producer": "daily_close_writer"
    },
    "originals.close_result": {
      "kind": "close_result",
      "max_bytes": 4096,
      "producer": "close_result_writer"
    }
  },
  "launch_descriptor_fields": "instance launch role revision region",
  "launch_coordinate_fields": {
    "cloud_run_instance": "project location service revision_name instance_id",
    "launcher_start": "project zone instance_id vm_boot_id start_insert_id",
    "supervisor_start": "project location instance_id supervisor_boot_id child_start_sequence"
  }
}
```

`a3_startup_correlation`: exactly one record joins instance and launch with boot,
role, release and region. Independent start <= process_start_us <= recorded_at_us
< independent end. A missing, changed or unmatched record blocks its launch.
Independent events and carry-in build the expected-launch map before the first
startup page; startup records cannot enlarge it or the serving roster. Write the correlation
once, immediately after reading the descriptor and allocating the journal boot,
before boot checks and before any ready record. process_start_us is the measured
process start, recorded_at_us is this correlation write time. If descriptor read,
journal creation or retention fails, no replacement correlation is invented and
readiness remains unavailable; independent launch inventory retains the gap.

`a3_boot_check`: exactly one final adoption per criterion per boot; identical
and conflicting duplicates both block. `checks` has exactly the listed names,
each `pass|fail`; `inputs` has exactly the listed source roles, whose values are
bounded artifact IDs. All records join boot, role, revision, region, boot_at_us
and released config digest. Adoption is within this boot's interval. A failed
check/source stays failed with its original record retained. The criterion runner
writes one final adoption record as soon as its local checks and selected source
reads have finished, with completed_at_us sampled at that adoption. A measured
failed check uses fail and retains the same inputs/originals. A missing required
input or unfinished measurement cannot be represented as a completed check: keep
the available source records, emit no substitute success record, and leave that
criterion missing. Do not rewrite an earlier final adoption after a later retry;
a new boot is required for a new adoption set.

For `boot_transport`, `dependencies` is a list of 1..16 exact records:
`{boot,region,config_sha256,operation,resource,attempts,marker_matches,elapsed_us,
deadline_us,observed_at_us}`. Operation is `transport|read`; attempts is 1,
marker_matches is true, and 0 <= elapsed_us <= deadline_us <= 10000000.
The independent released configuration has `dependencies.router` and
`dependencies.enclave`, each a list of `{operation,resource,region}`. Compare the
exact sets, with no duplicate observations, and require this boot, region and
config digest. Missing, duplicate, unrelated and wrong-role targets block.
Observation starts no earlier than this boot, completes before adoption, and
uses the qualified UTC relation; the retained elapsed interval is inside it.

For router `observer_freshness`, parse HEALTH_BASIS with its existing validator,
require reader_boot and installed source state. Its actual read_at_ns and the
record's installed_at_ns must lie between this boot and adoption; read completion
must precede install. installed_at_ns - read_at_ns equals install_elapsed_us *
1000, bounded by 250000 us. A contender also retains parsed HEALTH_CLAIM and
HEALTH, validates their publication join, owner boot, actual claim and publication
clocks within this boot and before installation, with publication <=500000 us.
For enclave, use exactly `router_path={ready,resource,router_boot,region,
observed_at_us}`, configured_router_region, local_transport_ready and echo_ready.
All ready booleans are true; the path resource/region equals released config's
`router_path={resource,region}` and the observation is within this boot before
adoption. Router health fields do not appear on enclave records.

For `marker_keys`, effective_pins exactly matches the independently retained
platform pins. Router pin keys are TR_ASYNC_SETTLE_ENABLED,
TR_ASYNC_SETTLE_PROTECTION, TR_SETTLE_OUTBOX_FAST_DRAIN_ENABLED; enclave keys
are TR_ASYNC_SETTLE_NEGOTIATE, TR_ASYNC_SETTLE_SHADOW. Values are the declared
`false|true|on|off` strings and must match the platform. Policy/config/fixture
digests and full source revision match the release inputs. Parse the public
original exactly as `{authority_epoch,config_sha256,source_revision,descriptor_ids}`;
epoch is positive, and descriptor_ids equals the released config's bounded list
of public descriptor IDs. For enclave also parse the attested-image original
exactly `{revision,image_sha256}` and match released revision and config's
enclave_image_sha256. A filename plus a passing check is insufficient. The
native/release owner supplies the reviewed descriptor and image originals.

`a3_traffic_ready`: exactly one record per inventoried boot; at_us is strictly
after all six completion times and before interval end. The readiness coordinator
samples at_us and writes this record once immediately after adopting all six
successful checks, before enabling opted admission; it then waits for the durable
record acknowledgement. Failed write or acknowledgement leaves ready unavailable,
without inventing another timestamp. `PreIngressRetention`
is runtime work: the router enforcement point is `Runtime.authorize_http` in
`src/trusted_router/services/async_settle_shadow_cohort_runtime.py`, before its
call to `authorize` creates the opted handoff fields. The release must also gate
the enclave opted-request admission handler before accepting its first opted
request; the enclave runtime owner must bind that exact callable in the native
release review (`EnclaveOptedAdmission`, not present in this package). Keep opting requests out
until the six checks succeed and the validated ready record has a durable
acknowledgement in the reviewed retained channel. Local stdout emission alone
is insufficient. Retain the ready original and acknowledgement for >=30 days.
The offline collector checks retained order; it cannot enforce this admission
point or turn a ledger parser pass into evidence that ingress was blocked.

`a3_opted_attempt`: result is exactly accepted, failed, rejected or timeout.
The attempt recorder samples `at_us` at the actual attempt/admission start being
checked, at the same event boundary used by the native receipt writer's start
event. It preserves that value while the attempt runs. This is **not** receipt
storage time, terminal-result time, or projected-record write time. The original
event-time join is `PAGE.events[selected].event_at_us == attempt.at_us`.
The recorder writes the single result projection when the result is known and
the page containing that start event has been retained; accepted means admission
accepted, not eventual settlement. Failed/rejected/timeout results retain the
same start coordinate. Later result emission, including after midnight, keeps
the start day's receipt coordinate and is captured into that event day's input;
the capture owner must establish delivery closure before calling the day complete.
If the process ends before result emission or the receipt page is unavailable,
retain the native originals and leave the attempt join missing. Do not stamp a
later completion event with a new start time or make up a receipt reference.

`receipt` is exactly `{day,boot,sequence,original}`. In the emitted form original
is the receipt_page coordinate above; in the captured form it is FILE_REF.
The retained original is the full **v2 PAGE** defined by
`async_settle_shadow_v2_evidence.PAGE` (65536 bytes, 1..128 events), with exactly
`v,policy_version,boot,day,first_seq,last_seq,previous_page_sha256,events`.
Use its existing parser, including its event validators and page/locator joins;
no reduced `{boot,sequence}` record is a receipt page. Select the unique event
whose numeric `seq` equals receipt.sequence. Require its non-null receipt_locator
(the native start event), PAGE.boot == receipt.boot == attempt.boot, and
PAGE.day == receipt.day == the collected UTC day. The parser joins the locator's
boot/day/first_seq/event_seq to the page and selected seq. Sequence is a JSON
integer from 1 through 9223372036854775807, compared exactly to the native
20-digit positive seq/event_seq without rounding or truncation. Native values
outside the projection's integer range remain missing, never wrapped. Zero,
empty bytes, malformed pages, missing selected events and changed coordinates
are missing joins, even if a FILE_REF still matches the file's bytes.

The event time must equal at_us, lie within the inventoried boot interval, and
be strictly after readiness. Every attempt, including malformed/unmatched and
failed attempts, remains in attempts.json and the denominator; missing joins
and attempts at or before ready block the day. Packaging retains status.json,
its missing states, original pages and attempt/receipt references. NativeExport's
full original receipt population must still be reconciled externally; this
projection cannot prove there were no absent attempts.

`a3_shutdown_span`: clock is exactly monotonic_us, with
`clock_origin={boot,monotonic_at_boot_us}` in this process's monotonic epoch;
no wall-clock subtraction. Outcome is graceful|hard_kill and complete is boolean.
`spans` has exactly preparation, export_wait, final_pass, close, result; each is
an ordered pair of integer monotonic microseconds at/after origin. Preparation
and wait are contiguous with final pass; close then result are contained within
final pass. Join boot, instance, launch, revision, region, day and interval end.
Missing spans stay missing. Retain actual measured close/result originals from
NativeExport, not inferred acknowledgements. Independently enforce close <=2 s,
result <=2 s, close + result <= final_pass <=4 s and preparation + wait + final_pass
<=min(actual remaining grace,9 s). Nested transactions are counted once.
Gate aggregate corrections belong to the gates package; C1 retains these actual
measurement semantics.

The close coordinator writes one final a3_shutdown_span per inventoried router
writer/day interval after the measured final pass has ended. Trigger it for
**each daily UTC close**, even when the process continues into the next day,
and for **termination** of the current interval. Set interval_end_us to the
independent interval end (UTC boundary or process end), not the later log-write
time. A close that finishes after midnight still belongs to the day it closes.
When termination coincides with a daily boundary, one record covers that ending
interval; do not emit a duplicate or an empty next-day interval. A running process
that spans several days emits a separate daily record for each closed interval.
The same measured phases apply to daily and terminating closes; actual measured
zero-length phases are allowed, absent phases are not zero.

Write after the close/result operations finish or their bounded attempt ends.
If all five measured endpoint pairs exist but the close is incomplete, write
complete=false with those actual pairs and retain available NativeExport
originals. If any endpoint is missing, there is no well-formed final span:
retain the partial original observations and leave the final record absent;
never fill in a zero or a guessed endpoint. Missing/partial records keep the
expected writer/day entry missing. A whole-process loss may prevent any final
write; independent lifecycle records retain that interval and its missing span.
A hard_kill outcome can only come from a retained observer with those actual
measurements; the absent process cannot report its own later end. The collector
keeps such an outcome incomplete. Both daily and terminating closes require the
separate closure/result originals and coordinate joins above; missing originals
remain missing even when the timing record is complete.

## External capture and release inputs

| Input | Owner | Exact join |
|---|---|---|
| Binding, original termination-before-correlation and restart witnesses, delivery boundary, carry-in | Native platform capture owner; release reviewer | Named resource/day and adapter version; carry-in source FILE_REF plus scoped launch; binding process/delivery qualifications bind exact originals and review |
| Cloud Run revisions or MIG membership/templates, retired members, grace and effective pins | Deployment capture owner | Platform revision name to full released SHA, role/region and instance; reviewed original FILE_REF; per-boot actual remaining grace |
| Complete lifecycle pages, operation starts/completions, launcher and startup originals | Native capture owner | Exact resource/day/token chain; insertId, operation.id, instance/launch, original bytes; no unknown kind discarded |
| Monitoring originals | Monitoring capture owner | Exact service/revision/VM and day plus original FILE_REF; corroborates coverage only |
| CI and native trial originals | CI and trial owners | Selected artifact ID, criterion/source role, router_revision, enclave_revision, config digest, region, original start/completion and FILE_REF |
| Policy, released config, fixture and full released revisions | Release owner | pins policy/config/fixture FILE_REFs, role release SHA; config-to-dependency and router-path mapping, descriptor IDs and image digest |
| Public descriptors, epoch and attested-image originals | Release and enclave capture owners | Parsed epoch/config/release and descriptor IDs; image digest and released revision |
| Boot clock relation and local observation originals | Runtime capture owner; release reviewer | boot and boot_at_us, qualified UTC/monotonic relationship; actual read/install/claim/publication/observation coordinates |
| Previous ledger originals, when continuing a boot | Ledger packaging owner | Exact boot/role/revision/region/boot_at_us, prior FILE_REF digest |
| Original receipts, close and result originals | NativeExport owner | Receipt day/boot/sequence; close day/boot/revision/region/interval; result original closure digest and outcome |
| Review records and released input graph | Release reviewer | APPROVAL subject digest and original REVIEW_RECORD; independently retained release-plan digest and all source FILE_REFs |

`NativeClients`, `LauncherOriginParser`, `LauncherLaunchDescriptor`,
`RouterDayBoundary`, `LauncherDeliveryBoundary`, `BootClockQualification` and
`PreIngressRetention` remain named external seams. Missing native shapes, origin
values, descriptor delivery, witnesses, complete delivery/retention records or
runtime implementation block qualification. Unit tests are offline records,
not proof that these external procedures ran. Additional supervision is only the
fallback for a demonstrated remaining gap.
