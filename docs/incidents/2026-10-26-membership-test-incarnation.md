# Membership test read a previous incarnation

CI run 40298782771, attempt 1, failed `TestAMemberRowSaysWhatAProcessRuns`
at commit `12ef8719b` after 40 ms. A later rerun passed. The failed row was
epoch 1, `leaving`, not live, with `owner/frontdoor` roles. The process this
test starts also has the `auditor` role.

## Cause

Service tests shared the `spike` database. Closing a listener releases its
ephemeral port, but stopping its process intentionally leaves its membership
row. Another test can receive the same address from `net.Listen`.

The `start` test helper launches `Run` asynchronously. Until `Store.Join`
commits, `NodeStatus` can legitimately read the previous process's row.
The test treated every existing row as the new process's row and failed
immediately on the predecessor's roles/state. It never checked the epoch.

`Store.Join` atomically replaces the row with the next epoch, new roles,
`serving` state, and commit timestamps. Heartbeat expiration changes `Live`,
not roles or state. Neither a fake owner clock nor heartbeat cadence explains
the old roles and `leaving` state in the first 40 ms. The existing membership
contract permits the observation; no production writer change is needed.

## Reproduction and repair

- A private emulator used the same pinned image as CI, with cloud credentials
  unset and `GOCACHE` under `/private/tmp`.
- The unmodified test passed 20 race-enabled repetitions with `-cpu 1`, and
  another 20 with a separate CPU load process. Port reuse was not guaranteed.
- Seeding an expired, leaving predecessor at the tested address reproduced
  the same failure in all 20 repetitions of the old assertion.
- Cases now have separate databases and explicitly cover both a new address
  and a reused address for the admission/auditor and auditor-only processes.
- The reused case verifies the predecessor, permits only its unchanged old
  epoch during startup, and then requires exactly the next epoch, correct
  roles, `serving` state, and liveness. Wrong roles/state at the new epoch
  still fail immediately. An unexpected epoch also fails.
- The fixed test passed 20 repetitions with `-cpu 1` and 40 more with
  `-cpu 1,4` plus a separate CPU load process, all with `-race`.

No new sleep, retry loop, timeout increase, or production configuration change
was added. The existing bounded startup wait now checks the incarnation it
was meant to observe.
