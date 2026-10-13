# A3-C1 runtime harness interface, proposal version 1

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
No arbitrary text, attached objects or extra result descriptions are allowed.

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
  }
}
```

`a3_startup_correlation`: exactly one record joins instance and launch with boot,
role, release and region. Independent start <= process_start_us <= recorded_at_us
< independent end. A missing, changed or unmatched record blocks its launch.
Independent events and carry-in build the expected-launch map before the first
startup page; startup records cannot enlarge it or the serving roster.

`a3_boot_check`: exactly one final adoption per criterion per boot; identical
and conflicting duplicates both block. `checks` has exactly the listed names,
each `pass|fail`; `inputs` has exactly the listed source roles, whose values are
bounded artifact IDs. All records join boot, role, revision, region, boot_at_us
and released config digest. Adoption is within this boot's interval. A failed
check/source stays failed with its original record retained.

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
after all six completion times and before interval end. `PreIngressRetention`
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
`receipt` is exactly `{day,boot,sequence,original}` with the types above and the
bounded original reference. Here sequence is the numeric value of the native
receipt's 20-digit event_seq, joined without rounding or truncation; the capture
owner retains that original coordinate and bytes. The boot and day join the inventoried interval and
native receipt coordinates. Every attempt, including malformed/unmatched and
failed attempts, remains in attempts.json and the denominator; missing joins
and attempts at or before ready block the day. Packaging retains status.json,
its missing states, original pages and attempt/receipt references. NativeExport's
original receipt population must still be reconciled; this optional projection
cannot prove there were no absent attempts.

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
