# Shadow deadline and SDK retry integration

## Evidence

On October 10 the serving `e007790` release still emitted regional shadow
read deadlines at 203-212 ms and commit deadlines around 855 ms. The regional
budget repair (#1677, merge `6c17ba2f`) passed branch and merge-commit CI but
was not serving at 21:46 UTC. The control-plane deployment was waiting behind
an attested gateway rollout. Do not treat these old-release failures as a
regression of the undeployed repair, or a resolved alert as proof of recovery.

The separate SDK reset repair (#1668) conflicted with that change. Integrating
it with main reproduced nine stale timeout assertions: they still expected
200 ms reads and a 500 ms transaction-control cap. The production reset guard
itself remained compatible with the new shared-deadline policy.

The real Spanner Session/Transaction retry path supplies another failure
mechanism. On an exact InternalServerError with either SDK transport-reset
marker, its private retry helper sleeps two seconds before checking the next
attempt. An RPC timeout alone cannot bound that sleep. In the integrated
negative control, removing the reset conversion produced six failures:
ordinary shadow work took 2.46-2.47 seconds; regional flushes took 2.80-2.94
seconds. This reproduces a latent worker-budget defect, not proof that every
production API-failure alert was a transport reset.

Further real-SDK testing found standalone shadow snapshot reads were not
inside the shared deadline context. The SDK restarts streaming reads on
ServiceUnavailable or resumable InternalServerError even with `retry=None`.
Four regressions reproduced success after an expired deadline: five 80 ms
attempts exceeded a 200 ms operator budget, and five 320 ms attempts exceeded
the one-second worker budget. Keeping the deadline context active while
consuming the stream makes each restart use only the remaining budget.

## Repair and guards

- Preserve #1677's 500 ms read cap and one-second shared transaction horizon.
- Preserve #1668's content-free reset conversion for Commit, Begin, and
  Rollback only in explicitly per-RPC-capped contexts with insufficient time
  for the SDK sleep. Ordinary billing retry behavior stays unchanged.
- Update exact RPC-budget expectations to the remaining shared deadline.
- Apply the shared deadline throughout standalone snapshot iteration, including
  SDK restarts, without extending any enclosing transaction deadline.
- Test 250/320 ms regional reads with 300/600 ms commits through the real SDK,
  with and without resets. Reads plus commit must share one second; an
  over-budget commit fails closed and must not create another retry sleep.
- Keep unknown commit outcomes, retention fences, idempotency, evidence gaps,
  queue bounds, and strict production alarms intact.

## Release verification

Do not bypass the cross-cloud rollout coordinator to clear the deployment
queue. Verify the eventual traffic-carrying source SHA contains both repairs,
async settlement remains disabled, new comparison evidence persists, and
regional diagnostics recover over a post-deployment observation window.
Cost-free healthy HTTP checks alone do not prove the background writer is fixed.

Validation results are recorded on #1668 for the exact integrated commit.
