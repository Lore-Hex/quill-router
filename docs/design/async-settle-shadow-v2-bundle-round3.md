# Round 3 captured-source packaging

This addition uses only local captured records. It adds no runtime imports, jobs,
flags or release actions. The existing files from rounds 1 and 2 are unchanged.
The new command module is `scripts.async_settle.round3.shadow_v2_window`.

The plan selects the NativeExport bundle/accounting files, the three C1 output
records, the Budget input record, the ExternalPack input record and History.
Collection checks every supplied producer and its nested file references before
building the day. An old assembly recipe supplies no missing producer.

The day composition follows C1 readiness `proofs` to the original reader checks,
then reads the named basis records. Batch presence cannot create a reader demand;
a named absent page remains missing. The derived inventory, coverage, roots,
budget envelopes, external results and history enter one day manifest/proof pair.
Collection prepares the daily review subject automatically.

The captured Budget path compares native operation originals with the actual
counter and publisher traces. Both envelopes retain every plan file reference,
including every PEAKS source, configuration, CPU series, allowances, shutdown
records and the comparison records. An absent component has no zero fallback.

External procedure assembly selects the exact downloaded archive member, compares
its run/job/artifact metadata and full revisions, derives the required check set,
and reads the original completed procedure records. Native trial records must
name their configuration, interval, resource and original operations. A job title
supplies no conclusion. The pre-flip `live` and `rare_cases` scopes additionally
require drain/handover records.

The command sequence is:

```sh
.venv/bin/python -m scripts.async_settle.round3.shadow_v2_window collect \
  --plan CAPTURES/plan.json --stage pre --out COLLECTED
.venv/bin/python -m scripts.async_settle.round3.shadow_v2_window review-inputs \
  --plan CAPTURES/plan.json --stage pre --input COLLECTED --out REVIEW
# Supply the recorded daily reviews in the plan and their original files.
.venv/bin/python -m scripts.async_settle.round3.shadow_v2_window pack \
  --plan CAPTURES/plan.json --stage pre --input COLLECTED --out PACKED
```

`review-inputs` exits 1 with `ReleaseReview` pending. `collect` and `pack` report
completion of file assembly. That status is not a gate result. The acceptance
capture uses real writer/exporter calls with explicit fake platform, CI and
measurement originals and test review records. Its short interval does not cover
the 48-hour forward horizon or seven days.

## Outstanding work

The current producer still materializes its native day transport and record
indexes. Large released file originals are split on disk, but this is not yet
complete bounded producer composition. The largest configured day has not been
measured; the short-capture memory result must not be substituted for it.

The 53 original-byte matrix cases exercise the requested stage for each family
and compare exact current-loader names, subjects and counts. Several cases still
share broader source/reference failures. They do not yet establish isolated
prerequisite effects or verified corrected-loader expectations. This remains
unfinished C2 acceptance work.

The gates owner has separate interface work: the frozen whole-day exact-dict
boundary, binary archive file input, and the current stage-policy/check-name
interface. The author report records exact locations and observed outcomes.
These boundaries do not remove C2's outstanding producer and matrix work.
