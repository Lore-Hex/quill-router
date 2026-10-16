# A3-C2 round 2: captured originals and measurements

This addition describes the new round-2 modules. The original bundle document
and all contracts, fixtures, gates, exporters, collectors and runtime files are
unchanged. These tools read supplied local files. They do not start trials,
release revisions, run jobs or create review decisions.

## Original records

`pack_streaming.OriginalSpool` writes one original at a time and retains a
SQLite index of path, digest, length and producer. INPUT_LEAF emission and checking
read one original at a time. `StreamingStageAssembly.capture` registers NativeExport,
Inventory, Readiness, Shutdown, Budget, ExternalPack and History outputs together
with their source, fixture and predecessor originals. Every nested FILE_REF must
match a registered original. A producer name with no original is a gap.

`split_transport` reads captured JSON incrementally. It splits children into
bounded transport parts, including a child whose JSON representation needs
several original-file pieces. All parts enter the existing multi-bundle input
plan. `restore_transport` checks every part and uses the exporter's existing
`SpoolArray` and `spool_transport` library functions. These parts are input
containers, not separate stage evaluations. Removing any listed part blocks
restoration. A3-E still owns loading the complete multi-bundle stage.

`pack_accounting_stream.compare_export_accounting_streamed` checks the original
NativeExport range/object plan, cursors, EOF, snapshot, lengths, digests, returned
records, generations, operation totals and failed passes. The transport comparison
retains record digests and lengths rather than a day's original bodies. Its
accounting metadata remains bounded by the existing per-pass limits. Each rerun
must be supplied as its own original pass; the release review must enumerate
those passes independently.

## References and measurements

`BasisReferenceIndex` starts from receipt predictions, samples and captured reader
references. It then resolves original basis batches and their predecessor links.
Deleting every batch cannot remove the expected population. An unrelated populated
batch or unnamed basis inside a demanded batch is a gap. Samples resolve their
own original receipt coordinates to the exact prediction basis digest. Captured reader records name day, boot, health_slot, basis_sha256
and page; the index does not manufacture reader records from batch presence.

`TerminalReconciliation` indexes the exact receipt coordinates, recomputes the
issuance decision, checks the terminal's original sample and matching retained
fields, and then invokes the frozen contextual join. It retains replay origins
and all dates in the original forward horizon, including support dates after the
selected window. It cannot reconstruct fields absent from the retained receipts.
The sample's workspace field and the receipt's workspace digest use different
constructions and are not compared as though they were interchangeable.

`monitoring_series` reads native `timeSeries.list` response pages, including
`timeSeries`, metric/resource labels, `points`, `interval`, `doubleValue` and
`nextPageToken`. The captured request page token must match the previous response.
Nanoseconds and original point boundaries survive. GAUGE points with no duration
use the explicitly reviewed sampling interval. Samples must cover the requested
interval exactly. Baseline and trial require the same resource, metric, labels,
interval and sample count. The comparison checks both per-position increase and
baseline peak plus reviewed increment, strictly below 45 percent.

`publisher_projection` implements section 15.5 configured arithmetic. Each PEAKS
component requires an explicit measured/configured original and field path.
`BudgetSourceAssembly` composes counter/trace/native-operation joins, CPU comparison,
PEAKS derivations, shutdown spans, allowance equality and the retained pre/post
producers. The pre checkpoint requires a closed isolated-trial post scope, with
its actual resource, interval and boot population. A native operation source
records complete day/boot/interval and individually identified operations with
logical count, actual RPCs, rows, bytes, duration and optional publisher slot.
This is an offline source adapter, not a claim that a native trial occurred.

`archive_members` preserves downloaded archive bytes in bounded pieces and selects
only explicitly listed exact member names. Both archive and member digests and
member lengths must agree; duplicate selected names are a gap. `check_scope`
records the section 11 and 15.6 groups independently of supplied results.
`scoped_result` requires the supplied check names to equal that derived set.
Later rollback checks have their own scope. The wrapper never establishes that
the external procedure ran: original run/job/artifact/result comparisons and
native source review remain necessary.

## Section 15.8 interfaces

The new front accepts these exact offline command forms:

```sh
uv run python -m scripts.async_settle.shadow_v2_window collect --plan "$V2_PLAN" --stage pre --out "$V2_DIR/pre"
uv run python -m scripts.async_settle.shadow_v2_window review-inputs --plan "$V2_PLAN" --input "$V2_DIR/pre"
uv run python -m scripts.async_settle.shadow_v2_window pack --plan "$V2_PLAN" --input "$V2_DIR/pre" --out "$V2_DIR/pre-approved"
uv run python -m scripts.async_settle.shadow_v2_window collect --plan "$V2_PLAN" --stage warmup --out "$V2_DIR/warmup"
uv run python -m scripts.async_settle.shadow_v2_window review-inputs --plan "$V2_PLAN" --input "$V2_DIR/warmup"
uv run python -m scripts.async_settle.shadow_v2_window pack --plan "$V2_PLAN" --input "$V2_DIR/warmup" --out "$V2_DIR/warmup-approved"
uv run python -m scripts.async_settle.shadow_v2_window collect --plan "$V2_PLAN" --stage window --out "$V2_DIR/window"
uv run python -m scripts.async_settle.shadow_v2_window review-inputs --plan "$V2_PLAN" --input "$V2_DIR/window"
uv run python -m scripts.async_settle.shadow_v2_window pack --plan "$V2_PLAN" --input "$V2_DIR/window" --out "$V2_DIR/window-approved"
```

`collect` uses the existing captured C1 entry point, the captured transport
splitter and producer registration. Plan `stages[pre|warmup|window]` names finite
`collections` (C1 capture-plan paths), `transports` (captured JSON paths), and
`inputs` (days, producers, sources, fixtures, predecessors). Paths are relative
to the plan directory unless `--input` is supplied. Re-running collectors does
not yet feed their new results into one newly derived DayPack graph.

`review-inputs` uses the supplied file/subject/criteria/whole-policy/verifier
plan and creates a new `review-inputs` directory beneath the input directory.
It returns incomplete while review is pending. `pack` with `captured_transport` emits a bounded multi-bundle input plan and
returns `MultiBundleLoading`. Otherwise it uses the round-1 captured
assembly recipe and supplied `release_plan_sha256`, with a byte-capacity check
before entering its whole-byte validator. There is no claim that the small
captured-plan examples constitute a complete first-window plan.

These exact action calls are named P6 seams and return incomplete locally:

```sh
uv run python -m scripts.async_settle.shadow_v2_window trial --plan "$V2_PLAN" --out "$V2_DIR/trial"
uv run python -m scripts.async_settle.shadow_v2_window release --plan "$V2_PLAN" --phase shadow-opt-in
uv run python -m scripts.async_settle.shadow_v2_window release --plan "$V2_PLAN" --phase shadow-opt-out
uv run python -m scripts.async_settle.shadow_v2_window probe --plan "$V2_PLAN" --phase warmup
uv run python -m scripts.async_settle.shadow_v2_window probe --plan "$V2_PLAN" --phase window
```

P6NativeQualification invokes the approved isolated qualification harness;
P6ReviewedRelease dispatches the reviewed workflow selected by the release plan;
P6DedicatedJob invokes the existing dedicated job. No new caller for these
procedures is implemented here. The existing P5 `shadow_report --policy shadow-v2`
command remains the stage evaluation command in section 15.8.

The uncaptured Monitoring call is
`monitoring.projects().timeSeries().list(name=<reviewed project>, filter=<exact metric/resource/labels>, interval_startTime=<baseline or trial start>, interval_endTime=<end>, view='FULL', pageSize=<reviewed bound>, pageToken=<previous nextPageToken>).execute()`.
Every page and original point must be retained; no automatic retries or guessed
missing samples. The real client is P6's MonitoringSeriesClient seam.

CI download calls remain bounded reads selected in the release plan:
`GET /repos/{owner}/{repo}/actions/runs/{run_id}`,
`GET /repos/{owner}/{repo}/actions/jobs/{job_id}`,
`GET /repos/{owner}/{repo}/actions/artifacts/{artifact_id}` and
`GET /repos/{owner}/{repo}/actions/artifacts/{artifact_id}/{archive_format}`.
CIArtifactClient owns capturing those responses and downloaded bytes; local
member selection is implemented here. No CI call runs in this package.

## Remaining boundaries

`FrozenDayValidationCapacity`: the unchanged contract requires an exact built-in
original-byte dict and exact candidate lists; PayloadGraph also caches raw files.
The front stops before exceeding its 4 MiB original-byte handoff. The traced
spool/accounting tests do not prove streaming DayPack validation. A3-C2 needs an
agreed bounded validation interface with the contract/P5 owners.

`NativeReaderReferenceAdapter`: normalize all native C1 reader-original shapes
into the explicit captured reader reference shape. This is A3-C2 work.

`StageProducerComposition`: registration, per-producer adapters and captured
recipes exist, but newly produced collector, Budget, ExternalPack and History
outputs are not yet fed into one complete stage derivation. This is unfinished
A3-C2 integration, not a missing external client.

`BudgetSourceAcceptance`: the new source-join composition still needs a complete
captured native-operation-to-pre/post-envelope acceptance case. This is A3-C2 work.

`A3IStageNegativeMatrix`: the real-writer source deletions document the present
cap loader defect. They do not provide all requested idle/killed-boot, six-criterion,
shutdown, retained-resolution, prerequisite-seed and new-region rendered vectors.
Those still need the complete stage graph (A3-C2/A3-I) and A3-E loader corrections.

A3-E's current cap-as-manifest result is kept exactly:
`evidence_schema_gap`, `transport/container`, detail `'router_writers'`, one loading
diagnostic. It prevents a report; it is not a frozen report failure count. This
expectation must be changed deliberately after adopting the corrected base.
