# Streams on the fast-path lease: turning them on, gate P, the way back

What `docs/design/fast-admission-streams.md` §5, §8.3 and §10 ask of an
operator. Two settings, both literal `false` in `scripts/deploy/rollout.sh`:

- `TR_FASTPATH_PILOT_STREAMS`: Python's pilot admits a stream the enclave
  heartbeats under a lease (§5.1's rule, below);
- `TR_FASTPATH_EDGE_STREAMS`: the edge may decide streams too. The edge
  policy carries `fastpath_edge_streams: true` only when it,
  `TR_FASTPATH_PILOT_STREAMS` and `TR_REAP_SNAPSHOT_BOOKING_ENABLED` are all
  on. It stays `false` until gate E (§8.5) passes and increment 5 is pinned.

## Before `TR_FASTPATH_PILOT_STREAMS` moves

1. **Increment 2b on every owner and front door.** Check each node's
   running release as `fastpath-deploy.md` does under "Taking a pinned
   release": every member reports increment 2b's release (#2212) or later.
   Python sends `EchoGranted` on every heartbeat after a stream's first, and
   a node from before increment 2 refuses it; before 2b an auditor that
   drains a lease reaps a stream without the reap floor
   (`fastpath-deploy.md`, "Streams on the lease: the release order").
2. **Python's increment 3 deployed** in every control plane region (the
   terminals after a reap and the reaper's lease reconcile, #2210).
3. **`TR_REAP_SNAPSHOT_BOOKING_ENABLED=true`** in `rollout.sh`; without it
   every stream passes over as `stream.snapshot_booking`.
4. **The alerts applied**: `TR Fast path control plane: heartbeat_divergence`
   and `...: settled_without_intent` (`infra/fastpath_control_plane_alerts.tf`,
   `fastpath-alerts.md`).

Then set `TR_FASTPATH_PILOT_STREAMS=true` in `rollout.sh` (the pilot
workspaces and region are the pilot's own) and deploy.

## Which streams join

A stream in the pilot's cohort joins only when (§5.1):

- `fastpath_pilot_streams` is on (`stream.switch` otherwise);
- Stage D's reason is `ok` (`stream.stage_d:<reason>`): eligibility on,
  heartbeats on, an accepted boot, `chat.completions` or `responses`, every
  candidate in credits, standard pricing, no priority or auto tier;
- the boot declared usage heartbeats (`stream.undeclared`);
- `reap_snapshot_booking_enabled` is on (`stream.snapshot_booking`);
- its reservation is its estimate (`stream.hold`, the tripwire).

A stream passed over is logged as `fastpath pilot passed over reason=<one of
those> workspace_id=...`.

## Gate P (§8.3)

A count gate, no calendar minimum (Joseph's decision of 2026-10-30): 1,000
leased streams settled or reaped with zero divergence, and over those:

- `fastpath.heartbeat_divergence` zero (the metric
  `logging.googleapis.com/user/fastpath_control_plane_heartbeat_divergence`),
  and no `invalid`;
- no `fastpath pilot winner's charge differs` line;
- every `fastpath leased reap recorded` cost equal to the running charge
  Python prices from the row's last snapshot, but for design §7 risk 2;
- no leased intent pending past the drain's overdue allowance, every
  `fastpath leased reap disposition unanswered` explained, and no
  `fastpath.leased_reap_overdue` or `fastpath.settled_without_intent` left
  unexplained;
- the heartbeat 503 rate and first-heartbeat p99 within 1.5x Python's own
  over the same streams, and no stream stopped by a front door `retry`
  outside a deploy or a stop;
- every `fastpath pilot terminal after a reap` line explained;
- streams cut by `deadline_passed` counted against Python's
  `heartbeat_lost` rate.

Find the lines with `gcloud logging read 'resource.type="cloud_run_revision"
AND textPayload:"trusted_router.fastpath"'`.

## Gate E (§8.5)

With increment 5 deployed on the edge and `TR_FASTPATH_EDGE_STREAMS` still
`false`, the edge composes every stream Stage D lets in for its decide
shadow alone (`body.stream` is its decision gate, so the shadow line's
`decide` map counts it under `body.stream`) and compares Python's 200 and
rows with it. A count gate, no calendar minimum: e2-decide's I2 bar over
those compared streams, and over them zero `differ.candidate_prices`,
`differ.cap_micro`, `differ.candidate_cost_reporting`,
`row.pricing_snapshot`, `row.heartbeat_seq` or `row.estimated_microdollars`
in the shadow line's fields. A stream Python's pilot did not lease is
compared on its 200 alone (rows `python_not_leased`). Then set
`TR_FASTPATH_EDGE_STREAMS=true`: the decide rows choose where the edge
answers streams, each one still gated on Stage D, the boot's declaration
and the hold (`stream.stage_d:<reason>`, `stream.undeclared`,
`stream.hold`).

## The way back, fastest first

1. `TR_FASTPATH_EDGE_STREAMS=false`, if it is on: the edge stops deciding
   streams within the policy's 60 s.
2. `TR_FASTPATH_PILOT_STREAMS=false` and deploy: no new stream is leased.
   Streams already leased keep heartbeating their owners and settle through
   the lease; the heartbeat route follows the row, not the setting.
3. To go further back than that: wait out a hold's life (2 h 20 min) plus
   the grace plus the drain's overdue allowance with no leased stream open.
   Until then no node goes below increment 2b and no control plane below
   increment 3.
