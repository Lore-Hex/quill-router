# PR3 shadow validation

Base: `7fc31bd5`, branch `speculation/shadow-observation`. Changes are uncommitted.
No git writes, migration, production deployment or enablement was performed.
The native emulator gate is blocked: Docker is absent and no loopback Spanner
emulator is configured. Skips are not native SQL acceptance.

## Flag-off and ordinary-path proof

- Default and rollout pin: `TR_SPECULATIVE_PROVIDER_SHADOW_ENABLED=false`; all
  rollout cohort lists are empty. The old schema is sufficient to deploy off.
- A subprocess import check confirms off app construction imports neither the
  observer nor its Spanner adapter. Off scope, worker/signer startup, schema
  access and refresh body/storage access are separately guarded.
- Only authorize owns an observer, after the completed timing snapshot.
  Nested async/sync calls emit once; settle, refund and heartbeat never emit.
- The existing ordered RPC suite runs in both modes, preserving **5 warm
  operations / 6 with the transactional pause gate**, with and without boot
  authentication. It also retains raw/federated/BYOK/replay/denial and retry
  assertions. No edits were made to the two RPC-diet owner files.
- Deterministic off/on transcripts match status, headers, complete response,
  ordered SQL and parameters, holds and balances through authorize, replay,
  settle and refund, including queue overflow and observer failure.
- Blocked worker IO does not block submission; an extra boot read makes the
  warm operation matrix red. Worker ContextVars, RPC counters and budgets are independent. The authenticated
  status endpoint exposes separate worker/refresh RPC totals, queue depth and
  sticky coverage loss.
- Projection mutations can name only the eight shadow tables. Grants use
  the frozen shadow type, separate issuer purpose and exact v1 fields; real
  verification and descriptor dispatch both reject them.

## Gates

Commands use `UV_CACHE_DIR=/private/tmp/astra-r3-uv` because the managed sandbox
cannot write the default uv cache. No permission escalation was used.

```text
uv run ruff check .
All checks passed!

uv run mypy src/trusted_router
Success: no issues found in 382 source files

uv run mypy
Success: no issues found in 382 source files

New shadow suites (final source/policy/endpoint guards)
57 passed

Final-source shadow, off/on RPC and response-timing suites
201 passed, 614 warnings in 92.88s (0:01:32)

Stage D, boot, outbox, ordered RPC/timing and conformance regression selection
2849 passed, 1068 skipped, 11 xfailed

SQL expression/type inventory and migration schema extraction
Final SQL inventory and extracted schema match.

Native GoogleSQL emulator execution
BLOCKED: docker executable absent; no configured Spanner emulator.

Full pytest with four workers, no cacheprovider, task-owned basetemp and coverage
8 failed, 16835 passed, 1118 skipped, 12 xfailed, 11517 warnings in 3724.98s (1:02:04)
Required test coverage of 70% reached. Total coverage: 84.92%

Final-source rerun: three SQL guards and the BYOK property test
4 passed, 6 warnings in 31.95s

Final adapter-boundary and service-surface audit suites
111 passed, 62 warnings in 5.98s
```

The full-run command also adds `--cov=trusted_router --cov-report=term
--cov-fail-under=70`. The completed full-run basetemp and targeted test directories were deleted
after their processes exited. The full run was not green and is not presented
as a passing rerun. Final-source checks below cover its non-baseline failures.

## Full-run failure disposition

The full run began before the last source/telemetry edits. Its raw log is
`/private/tmp/astra-r3-full.log`; the final-source matrix is
`/private/tmp/astra-r3-final-matrix.log`. No second full run was performed after
the final audit-only edits.

| Failed gate | Cause and final verification |
|---|---|
| `test_unregistered_sql_fails_in_a_copy` | Source/manifest changed during the run; final-source rerun passes. |
| `test_new_builder_fails_in_a_copy` | Same stale shadow SQL scope fingerprint; final-source rerun passes. |
| `test_spanner_sql_inventory_is_complete` | Dispatch inventory changed during the run; final-source rerun passes with all expressions and sinks registered. |
| `test_cloud_sdks_are_confined_to_the_adapter_layer` | Added the new native adapter to the explicit allowlist, with its ShadowStore-port justification; full boundary suite passes. |
| `test_internal_surface_route_inventory_matches_capability_audit` | Registered both new authenticated routes in the exact route set and capability audit; full service-surface suite passes. |
| `test_v2_still_rejects_a_wrong_binding` | Hypothesis deadline exceeded (301.69 ms versus 200 ms) during the covered four-worker run. Passes unchanged on final source and exported base. No deadline or test weakened. |
| `test_a_pep695_type_alias_annotation_is_not_enumerated` | Known Python 3.11 parser failure; independently reproduced on unchanged `7fc31bd5`. |
| `test_typed_billing_store_helper_unwraps_the_module_proxy` | Known Python 3.11 runtime Protocol behavior; independently reproduced on unchanged `7fc31bd5`. |

Base reproduction: **2 failed** for the two declared Python 3.11 cases;
**1 passed** for the BYOK property test. The base was exported with read-only
`git archive`, tested separately and deleted. No branch, index or commit writes.
Native emulator execution remains blocked, independently of these results.

## Mutations

Each edit ran in a temporary source/test copy with a passing baseline and a
passing restored baseline. The money variant actually substitutes lifetime
top-up for paid headroom; the storage variant names `tr_credit_balance`.
Full commands and gate names are in `tests/speculation_shadow_mutations.py`;
per-mutation receipts are in `docs/speculation-shadow-mutations.json`.

| Mutation | Result | Baseline/restored |
|---|---|---|
| duplicate callback counts | red | pass/pass |
| current request qualifies | red | pass/pass |
| submit waits on worker IO | red | pass/pass |
| extra synchronous boot read | red | pass/pass |
| success clears sticky loss | red | pass/pass |
| lifetime topup treated as paid | red | pass/pass |
| real grant type | red | pass/pass |
| real store namespace | red | pass/pass |
| first batch identity reused | red | pass/pass |

## Exact additive DDL

Migration precedes **enablement only**, not the flag-off binary deployment.

```sql
CREATE TABLE tr_speculation_shadow_event (
    plane STRING(32) NOT NULL,
    producer_incarnation STRING(128) NOT NULL,
    sequence INT64 NOT NULL,
    body STRING(MAX) NOT NULL,
    updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)
  ) PRIMARY KEY (plane, producer_incarnation, sequence);

CREATE TABLE tr_speculation_shadow_success (
    plane STRING(32) NOT NULL,
    identity STRING(128) NOT NULL,
    body STRING(MAX) NOT NULL,
    updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)
  ) PRIMARY KEY (plane, identity);

CREATE TABLE tr_speculation_shadow_scope (
    plane STRING(32) NOT NULL,
    identity STRING(128) NOT NULL,
    body STRING(MAX) NOT NULL,
    updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)
  ) PRIMARY KEY (plane, identity);

CREATE TABLE tr_speculation_shadow_producer (
    plane STRING(32) NOT NULL,
    identity STRING(128) NOT NULL,
    body STRING(MAX) NOT NULL,
    updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)
  ) PRIMARY KEY (plane, identity);

CREATE TABLE tr_speculation_shadow_paid (
    plane STRING(32) NOT NULL,
    identity STRING(128) NOT NULL,
    body STRING(MAX) NOT NULL,
    updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)
  ) PRIMARY KEY (plane, identity);

CREATE TABLE tr_speculation_shadow_route (
    plane STRING(32) NOT NULL,
    identity STRING(128) NOT NULL,
    body STRING(MAX) NOT NULL,
    updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)
  ) PRIMARY KEY (plane, identity);

CREATE TABLE tr_speculation_shadow_grant (
    plane STRING(32) NOT NULL,
    identity STRING(128) NOT NULL,
    body STRING(MAX) NOT NULL,
    updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)
  ) PRIMARY KEY (plane, identity);

CREATE TABLE tr_speculation_shadow_exposure (
    plane STRING(32) NOT NULL,
    identity STRING(128) NOT NULL,
    body STRING(MAX) NOT NULL,
    updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)
  ) PRIMARY KEY (plane, identity);
```

## Files and git status

Final uncommitted file list (`git status --short`):

```text
 M docs/operations/internal-surface-split.md
 M scripts/deploy/rollout.sh
 M src/trusted_router/config.py
 M src/trusted_router/gateway_timing.py
 M src/trusted_router/main.py
 M src/trusted_router/routes/internal/__init__.py
 M src/trusted_router/routes/internal/gateway.py
 M src/trusted_router/storage.py
 M src/trusted_router/store_protocol.py
 M tests/conformance/spanner_ddl.py
 M tests/conformance/spanner_schema_source.py
 M tests/conformance/spanner_sql_manifest.json
 M tests/test_cloud_sdk_boundary.py
 M tests/test_gateway_authorize_spanner_operations.py
 M tests/test_gateway_response_timing.py
 M tests/test_service_surface_routing.py
 M tests/test_speculation_protocol.py
?? docs/speculation-shadow-mutations.json
?? docs/speculation-shadow-runbook.md
?? docs/speculation-shadow-validation.md
?? scripts/deploy/migrate_speculation_shadow.sh
?? src/trusted_router/routes/internal/speculation.py
?? src/trusted_router/services/speculation_shadow.py
?? src/trusted_router/storage_gcp_speculation_shadow.py
?? tests/conformance/test_speculation_shadow_native.py
?? tests/speculation_shadow_mutations.py
?? tests/test_speculation_shadow.py
```
