# The fast admission path's production install, stage P0 of
# docs/design/fast-admission-production-rollout.md: the settle log and the
# record topic, the auditor's subscriptions, an archive of each topic, the
# nodes' identity and the envelope key's secret, and two nodes in
# us-central1 with no external address, the workspace allow-list empty, so
# nothing admits until W1's switch names a workspace. The schema is the
# deploy workflow's (scripts/deploy/migrate_fastpath.sh), not Terraform's.
#
# The nodes run the release named below: a commit on main whose CI passed,
# built and uploaded by .github/workflows/fastpath-release.yml. Changing it
# changes the instances' metadata only; a node picks it up when replaced or
# restarted, per docs/runbooks/fastpath-deploy.md.
locals {
  fastpath_region = "us-central1"
  # A main commit whose CI passed and whose binaries fastpath-release.yml
  # uploaded; a node picks a new one up when reset or replaced
  # (docs/runbooks/fastpath-p0.md). bfa94e22e: the merge of #1728, P1's run,
  # the first release carrying loadgen, fastpathwatch and load-mix.json.
  # 6709d268e: main after the edge's health answer (#1875); the first
  # release a node with the edge attributes may run. 04c5ad769: the merge
  # of #1970, the routing snapshot compiled as of its window's start, on
  # #1969's edge flags (-edge-snapshot-url, -public-key, -kid, -plane and
  # -edge-shadow); the first release a node with the snapshot attributes
  # below may run. Its fastpath/ tree is main's as of f12f8967d.
  # 2d90fa113: main after #2040, with G10's decide pipeline and shadow
  # (#2027/#2028), G11's decide path (#2030, off by flag), the reservation
  # commit timestamp (#2038) and G12's per-period counts (#2039); it takes
  # -edge-policy-url and -edge-decide-shadow, which the nodes' policy
  # attributes below pass (stage I1).
  # 7f392e79d: main after #2061 (G13), with #2065's hold split: the decide
  # shadow keeps requests carrying a cache affinity key, composes the cold
  # order and measures Python's answer as affinity_cold or affinity_warm,
  # so the soak compares the cohort instead of passing nearly all of it.
  # 892bd7593: main after #2098, with G14's v3 routing snapshot (privacy
  # tiers and parameter tables, so the evaluator answers min_privacy and
  # requested_parameters), G15a's optional async_settle and billing_valid,
  # and the auditor's call deadlines and stall watchdog.
  # 620e0ca00: main after #2118, with G15b's async compose (the edge
  # renders async-v1's additions), option A (the edge decides requests
  # carrying a cache affinity key in the cold order; Python's warm re-rank
  # counts as differ.affinity_warm) and G15c's async admission, dark while
  # edge_decide is false, with the shadow's per-population async counts.
  # fc1ed83bb: the merge of #2130, a deadline on each of the process's
  # startup calls (its ring join and first membership scan among them) and
  # a watchdog on its start and the session's warming that marks the node
  # unhealthy and exits, so systemd restarts it.
  # 0f0b1829a: the merge of #2133, the start before the service runs
  # bounded too: a deadline on each secret read, and a watch that exits the
  # process if it has not reached the service within 15 s per secret read
  # plus a minute.
  # cae6c5479: main after streams increments 5+6 (#2226) and the in-flight
  # 429 shadow fix (#2229): the edge tells the owner whether a decided
  # request streams, composes eligible streams, and decides them only with
  # fastpath_edge_streams on; the shadow counts Python's in-flight 429 to
  # an inflight.key pass as passed. The pilot leases streams. The edge
  # switch, off when this release shipped, is on since gate E (2026-11-02,
  # TR_FASTPATH_EDGE_STREAMS=true in scripts/deploy/rollout.sh, #2252):
  # the edge decides eligible streams for the workspaces whose decide rows
  # are on.
  # 328a3651c: the auditor's pending sweep reads a pack's staged records
  # in one call and works up to eight leases' packs at once, each lease's
  # in order (#2298); at 1,000 a second the packs took about 25 minutes to
  # clear one at a time.
  # 04b39913b: that sweep raised the billing store's CPU from 33% to 47% at
  # 1,000 a second, so the auditor remembers the leases it has found, sweeps
  # retired staged records once a minute, and runs two lease workers (#2303).
  # f1208a3d4: the stager groups the records that arrive while one staging
  # transaction runs into the next one, up to 256 records or 8 MiB, instead
  # of committing once a record (#2318).
  # 09c6a9398: a stream's hold is capped like any catalog request's, matching
  # Python (#2337; streams above the cap leave the cohort by the stream.hold
  # tripwire); every pass-through is timed, logging "fastpath edge pass" and
  # "pass slow" lines (#2331, #2344); a lone slow body read raises no
  # slow-pass alert (#2350); the money flags -required-tier, -allowance and
  # -floor, unset so defaults hold (#2354); the owner keeps an admission
  # ceiling nothing reads yet (#2347); records carry a stream's ceiling, with
  # RecordCeiling off, which needs tr_lease_hold.ceiling, live since
  # 2026-11-06 18:15Z (#2355); the fleet audit, off by default (#2356).
  # 5c5f11fad: a declared-length request body ends at its length, so the
  # transport's last read no longer cuts Python's answer short (#2358); a
  # workspace whose decide row is on but that the node's switch copy leaves
  # off passes at workspace.not_enabled, before the in-flight gate, where it
  # passed at owner.off; Go reads the policy's "all", dark until a policy
  # carries it, and the wildcard decide row, dark until one is written; the
  # decide line gains decided_via= and the shadow line distinct_keys= (#2359);
  # the shadow counts Python's in-flight 429 to any pass but inflight.key as
  # inflight_unchecked.<pass>, never paged (#2367: the 2026-11-06 03:55Z,
  # 18:27Z and 19:27Z edge_would_admit_python_refused pages).
  # 8af87998a: every stop writes the workspace's hold, and disable-all the
  # fleet hold, in the transaction that turns it off, which needs
  # tr_fastpath_workspace_hold and tr_fastpath_kill, live since 2026-11-07 06:35Z;
  # fastpathctl gains release, release-all, release-stamped, hold-backfill,
  # the enabler's pause, resume and arm, and the hold listing (holds);
  # nothing reads a hold to turn a workspace on yet (#2385, widening PR 4,
  # dark); RecordCeiling is on, so a stream admitted with a ceiling runs
  # past its hold to it and its records carry
  # ceil, and no gateway sends a ceiling until fastpath_stream_ceiling moves,
  # so the records match the release before (#2383, stream ceiling PR 3;
  # every node ran 5c5f11fad, which reads ceil, before this pin).
  # bd4398c21: the edge reads the policy's optional fastpath_stream_ceiling
  # member, refusing "false"; with it, a stream whose hold is below its
  # estimate is decided and admitted with that estimate as its ceiling
  # instead of passing at stream.hold (#2406, stream ceiling PR 4). Python
  # publishes the member only with TR_FASTPATH_STREAM_CEILING true, false
  # in rollout.sh, so the policy matches the release before; 8af87998a
  # refuses a policy carrying the member, so this pin comes first.
  # 51e8fabc1: model/provider/workload ranking with stable speed bands and
  # a ZDR preference (#2436). The reader requires ranking.model_workload;
  # upgrade both nodes before relying on the new routing order in the edge.
  # d23591a1f: main with edge tickets (#2506, #2510): the edge asks Python
  # for an async-v1 settlement ticket only for a workspace the policy's
  # async_settle.edge_tickets member names, which Python publishes only with
  # TR_FASTPATH_EDGE_ASYNC_TICKET_WORKSPACES set (rollout.sh). Every node
  # must run d23591a1f or later while that switch is set: an older release
  # refuses a policy carrying the member. d23591a1f also carries every
  # provider's async rules (#2451) and all-mode admission by request (#2466),
  # both already live on the Python side.
  # 949c757f6: takes -allowance-tier2 and -allowance-tier1 (#2542, #2545).
  # 1732a7a89: the edge reads the policy's optional async_settle.edge_leased
  # and overlap_workspaces members (#2590): with them, it answers an async-v1
  # authorize of a workspace async admission covers when it leases the
  # request itself, with Python's answer to a leased authorization (no
  # ticket off the overlap list). Python publishes the members only with
  # TR_FASTPATH_EDGE_ASYNC_LEASED true (rollout.sh; true since #2598, after
  # both nodes ran this release); 949c757f6 refuses a policy carrying them,
  # so this pin came first. Its other fastpath/
  # changes since 949c757f6 are tests and test data (#2555, #2586).
  # d3aa9c0a0: the edge reads the authorize body's gateway_request_id as
  # the schema declares it (#2603). The enclave sends the id with every
  # authorize since 2026-11-14, and 1732a7a89 takes it for an undeclared
  # member: it composes every async-v1 answer as ineligible (so asks for
  # no edge ticket), puts the id in the idempotency fingerprint, and
  # leaves the row's gateway_request_id NULL. This release answers as
  # Python does. The nodes take it one at a time in either order: a row
  # is read back only by the process that wrote it. A node still on
  # 1732a7a89 reports row.gateway_request_id in its decide shadow until
  # it is restarted. Its other fastpath/ change since 1732a7a89 is the
  # shadow's comparison of a leased answer (#2597).
  # 5133a83c6: the edge's ordinary requested parameters are Python's 26
  # (#2607): tools, a reasoning setting and the sampling parameters the
  # enclave reports keep an async-v1 answer eligible. Python answers so
  # since its own deploy of #2607. A node still on d3aa9c0a0 answers
  # such a request ineligible, the narrower way, and its decide shadow
  # reports async_shape for it until the node is restarted. The nodes take
  # the release one at a time in either order. Its fastpathctl takes
  # us-east4 and europe-west4 for the decide wildcard and, without -region,
  # stops and reads every wildcard region (#2610); the node binary's only
  # other fastpath/ change since d3aa9c0a0 is a test with comments (#2604).
  # daedb45ad: an eligible edge ticket's answer carries the request's
  # invocation_nonce, as Python's does (#2632). Without it the enclave
  # bound nothing to an eligible edge ticket and settled the request
  # synchronously; with it the enclave settles such a request
  # asynchronously, the first time any edge ticket binds. The switch for
  # that is the ticket list (TR_FASTPATH_EDGE_ASYNC_TICKET_WORKSPACES in
  # rollout.sh, `*` today): with the list empty the edge asks for no ticket
  # and answers what it leases without one. Nothing in Python or the
  # enclave changes for it. The decide shadow reads a workspace's credit
  # rows once after Python's credit refusal of a request the edge would
  # have answered, and pages only when a row is marked in debt, the rows
  # cover the hold, or the read fails (#2631). Its fastpathctl takes
  # us-west1 for the decide wildcard as well (#2612). The nodes take the
  # release one at a time in either order: a node on 5133a83c6 keeps the
  # earlier behaviour. Both nodes have run 5133a83c6 since they were reset
  # on 2026-11-16 at about 01:30Z; a node takes the pinned release at any
  # reset, planned or not.
  # b1d3dbcf9: the per-minute decide line gains float_at, passed_enabled,
  # passed_unknown, enabled_unknown and mix_dropped (#2650), tallied on one
  # background worker after the forward (only the first float's place is
  # found inline); nothing the edge answers, refuses, passes or evicts
  # changes. It also counts an eligible edge ticket the answer cannot carry
  # as unspliced (#2643). Its other fastpath/ changes since daedb45ad are
  # test data (model alias vectors) and a test comment (#2640). Both nodes
  # run daedb45ad (node 16 since 09:38Z, node 15 since 15:10Z on
  # 2026-11-16). The release is
  # b1d3dbcf9 rather than #2650's own merge (67f6442dc) because that
  # commit's CI hit an unrelated flaky test and published nothing; their
  # fastpath/ trees are identical. The nodes take it one at a time in
  # either order. Both nodes run b1d3dbcf9 (node 16 since 01:13Z, node 15
  # since 02:18Z on 2026-11-17).
  # 2918dbbcf: the settle log hedges a publish call the server holds: at 1,
  # 3 and 7 times the hedge delay (250ms, the release default;
  # fastpath-settle-hedge overrides it, "0" turns it off on a node whose
  # startup script passes the flag) it sends the same batch again, before
  # the batch's deadline, within a per-node budget and never for a batch over
  # the size cap (#2671). Every copy carries the same owner sequence numbers,
  # so the auditor skips a duplicate as it skips a republish's. The
  # nodes' startup script is not replaced on running nodes
  # (ignore_changes), so on 15 and 16 the off switch is pinning b1d3dbcf9
  # again. The other fastpath/ changes since b1d3dbcf9 are tests and test
  # data (#2670, #2677, #2661). The nodes take it one at a time in either
  # order.
  # fab9cd893: the edge decides authorize requests that carry
  # provider.max_price (#2689). A prompt and/or completion limit with no
  # sign or exponent, at most 9 integer and 6 fraction digits, filters
  # routes the way Python's _endpoint_matches_parameter_and_price_filters
  # does, read from the literal's digits; every other max_price shape still
  # passes to Python with its own evaluate.max_price.* reason. The other
  # fastpath/ changes since 2918dbbcf are catalog test vectors (#2675,
  # #2687). Off switch: pin 2918dbbcf again. One node at a time.
  # 541e687bf: the decide shadow settles a covered credit refusal against
  # Python's own read (#2711): Python's internal 402 names the read that
  # decided it (X-TR-Credit-Refusal-Read), and a refusal whose evidence is
  # short, with matching row counts and, for snapshots, a short unmarked
  # re-read at that read's timestamp, counts as funding_race and never
  # pages; every other covered refusal still pages with a race=<why>
  # reason, and edge_funding_race watches their rate. The shadow only
  # observes, so nothing Go answers changes. The other fastpath/ change
  # since fab9cd893 is a catalog test vector (the 2026-11-17T23:26Z price
  # refresh). Off switch: pin fab9cd893 again. One node at a time.
  # 65e856777: brings the nodes level with what Python has answered since
  # 2026-11-20 05:07Z. The decide shadow has differed on about 4% of
  # comparisons since then, nearly all on openai_safety_identifier, which
  # 541e687bf does not compose. Go changes since 541e687bf:
  #   - #2752: an authorize that negotiates X-TR-OpenAI-Safety: v1 and has a
  #     direct OpenAI candidate answers with the stable safety subject,
  #     derived from the key's creator or else the workspace owner; Go now
  #     reads the records' existing creator_user_id and owner_user_id.
  #   - #2732: every request of a workspace with BYOK keys passes to Python
  #     (candidates.byok), which alone reads their filters and order.
  #   - #2717: the uncensored GLM 5.3 Flash identifier resolves.
  # The other fastpath/ changes are test vectors and #2722's test race fix.
  # Off switch: pin 541e687bf again. One node at a time.
  # 5e3ebc8b7: the nodes learn hosted decision models and workspace origin
  # policies, with nothing switched on. Go changes since 65e856777:
  #   - #2794: the evaluator resolves an exact hosted decision model on
  #     route decide (no context-window step, as Python's decide resolver).
  #   - #2799: the edge reads two optional policy members,
  #     fastpath_edge_hosted_decide and async_settle.decide_route, and with
  #     the first may answer a hosted decide authorize itself, async-v1
  #     additions included. Neither member is published while
  #     TR_FASTPATH_EDGE_HOSTED_DECIDE is "off" (scripts/deploy/rollout.sh),
  #     so on this release every hosted decide still passes to Python. Such
  #     a pass is now labelled by the gate that made it, among them
  #     hosted_decide.off and hosted_decide.async_unknown, where it was
  #     evaluate.pass, and both shadows compare a hosted decide (the decide
  #     shadow only one without the async-v1 header). A decide authorize
  #     that needs a ticket and has no valid nonce passes to Python.
  #   - #2797: a workspace whose origin_policy is set to anything but
  #     unrestricted, or is malformed, passes to Python (workspace_origin),
  #     which alone filters candidates by it. A node on an earlier release
  #     does not read the field.
  # This release must be on every node before the hosted decide switch
  # leaves "off": a node on an older release refuses a policy that carries
  # a member it does not know. The other fastpath/ changes are tests and
  # test vectors (#2808 among them).
  # Off switch: first set TR_FASTPATH_EDGE_HOSTED_DECIDE to "off" and let
  # that roll out (65e856777 refuses a policy that carries either hosted
  # decide member), then pin 65e856777 again. One node at a time.
  # e1ed7b362: main after #2893. Its fastpath/ changes since 5e3ebc8b7:
  #   - #2892: a settle's full record may wait past PublishWait (250 ms)
  #     up to PublishMax (700 ms), borrowing the spare, the peer's wait and
  #     at most half the owner's inside the same AnswerWithin, so a slow
  #     record topic no longer answers settles failed; a call that budget
  #     cuts short is no evidence against its owner. Each node writes a
  #     per-minute "fastpath front door:" line that names why any terminal
  #     answered failed.
  #   - #2893: a pack's done-marking takes the lock it decides on
  #     exclusively first, so auditor members marking one lease's packs no
  #     longer abort each other on shared-lock upgrades. Members on either
  #     release may run together.
  #   - #2805: the hosted decide no longer passes every BYOK workspace to
  #     Python. With one key per provider again, the edge reads a
  #     workspace's BYOK providers as it did before multi-key BYOK. Live,
  #     as the hosted decide is on.
  #   - test vectors and a test-only port fix (#2865, the price refreshes).
  # The wire format is unchanged, so the nodes may run either release while
  # one is replaced. Off switch: pin 5e3ebc8b7 again. One node at a time.
  # ddde9504f: main after #2904. Its fastpath/ changes since e1ed7b362:
  #   - #2906: an owner notices a starved shard (no room under any lease,
  #     its top-up refused for the floor or allowance) under
  #     -starved-leases; this release's default `log` writes one line a
  #     minute per such shard and closes nothing.
  #   - #2903: the decide line names each key-fact cache miss's cause
  #     (cache_miss) and the inline read-through's outcome (cache_wait);
  #     the read-through itself stays off (-edge-decide-cache-wait 0) until
  #     a node carries the new startup attribute.
  #   - #2904: the edge can decide budget_strict keys with no limits, dark:
  #     it acts only once the control plane publishes the
  #     fastpath_pilot_strict_keys policy member, which waits for the
  #     product rule that a limitless strict key never gains a first limit.
  #   - test vectors (the price refreshes).
  # No decision changes until a switch is set. The wire format is
  # unchanged, so the nodes may run either release while one is replaced.
  # Off switch: pin e1ed7b362 again. One node at a time.
  fastpath_release        = "ddde9504f147db9d83402f9fb1cbb8d3820186a9"
  fastpath_release_bucket = "tr-fastpath-releases-${local.gcp_project_id}"
  fastpath_database       = "projects/${local.gcp_project_id}/instances/trusted-router-nam6/databases/trusted-router"
  fastpath_pubsub_agent   = "serviceAccount:service-${local.gcp_project_number}@gcp-sa-pubsub.iam.gserviceaccount.com"

  # The digests of each release a pin may name, copied from the release's
  # published SHA256SUMS when it is pinned (docs/runbooks/fastpath-deploy.md,
  # "Pinning a release"): fastpath, the node binary's sha256 as SHA256SUMS
  # lists it, and sha256sums, the sha256 of the SHA256SUMS file itself. The
  # nodes' startup script and the load generator's check what they download
  # against these (instance attributes fastpath-release-sha256 and
  # fastpath-release-sums-sha256) before installing anything, so a later
  # overwrite of the release bucket cannot change what a node runs. A pin
  # whose release has no entry here fails the plan (the precondition on the
  # nodes) and tests/test_fastpath_release_integrity.py. An entry stays while
  # a rollback may return to its release. b1d3dbcf9 is #2666's pin, listed
  # so that change and this one land in either order.
  fastpath_release_digests = {
    "ddde9504f147db9d83402f9fb1cbb8d3820186a9" = {
      fastpath   = "f8c0ae718e27acba890730426964017b3587f383fb0a686aa75096a323b28887"
      sha256sums = "3a3c52badd01b15063d18aa38b95eb6abfadc0da20fb47145a314b0df5fee876"
    }
    "e1ed7b362a83d74fb7a0fdf972c65f2b86ba68e2" = {
      fastpath   = "42eff1075063eb065ed35e86aca47b01a1a0626bc077238db35d8701f860f935"
      sha256sums = "1b625c5550d8f96d6515a064360f2ee721cc23bfbb6038e63d205824e94f3320"
    }
    "daedb45adc927a1e30e0d4ed41fe57c81aeccc76" = {
      fastpath   = "42e381dbe30a2380cd2bd6f6a556e09db2f2bafdeaa80b5d3dd310e3359e792a"
      sha256sums = "20d897793d7302991454cd11d5514883ce0fd529d38be91ce8bbbdb20843fd23"
    }
    "b1d3dbcf990d6f896a65c5fe1affc61e04eec923" = {
      fastpath   = "b5e11ff14dcd52dc7e5ec65b6bd077bcfec9ff413ebaab19fb9b392677855b60"
      sha256sums = "50311ae074fb78935ab124abaa29117119ae0722671116f0225c0a2bf9defb96"
    }
    "2918dbbcff831e82600ba23c0546cfeec49f70bd" = {
      fastpath   = "41f56868d8b3cce320da2ef14a1fcab28f159288d60d0e41a76215313d741c17"
      sha256sums = "b4af0b6b0f1c2272cdc74d94311af804537f1a8fbaf1e54b35bd01d2bcd68642"
    }
    "fab9cd893826ae6476e8ca407f7d3c5cfbf51ee3" = {
      fastpath   = "f2837b7c443c0319829fa17fa8c0097f01543064196d61c39e88e114dc18c304"
      sha256sums = "8b1aa4e1cd992421faa1ea37d6603c17590f51d613166b220b327eeabd808e4e"
    }
    "541e687bf8dba385338fab24d5dfaa16a72fe9be" = {
      fastpath   = "7934c7a03b12934cc9f0cfbaa3aac328d9184311d803fd6852809fb5cfb1466b"
      sha256sums = "ba630193172aae6ec8ad119d8d1ab163630f54d3cf58d41fdba1144fe18561c3"
    }
    "65e856777546e0d0c67f0c8d049c3d54ba63c443" = {
      fastpath   = "ecd6ce47f1b32c40bbbc6fce69c95d552296b6f234fb8e4841c259de3eb68ade"
      sha256sums = "2110c83771dce88b4bb57d600dba1679716534edbd60497ea3d067666f23510b"
    }
    "5e3ebc8b70cb1fbae57704d5cffca0ef3b9c5ca1" = {
      fastpath   = "1519270769ced33d35a54bdc3b36ab8380b95a035bbd83b572c45c640cd8ddf4"
      sha256sums = "dfb305a1fb69cd94f20de824dda4925e0373e77ef5a8b17163d25e1fe37c57e3"
    }
  }
  fastpath_release_sha256      = try(local.fastpath_release_digests[local.fastpath_release].fastpath, "")
  fastpath_release_sums_sha256 = try(local.fastpath_release_digests[local.fastpath_release].sha256sums, "")
  fastpath_release_digests_ok = alltrue([
    can(regex("^[0-9a-f]{64}$", local.fastpath_release_sha256)),
    can(regex("^[0-9a-f]{64}$", local.fastpath_release_sums_sha256)),
  ])

  # The edge (E1): its port, the control plane's internal surface in the
  # nodes' region (the Cloud Run service behind /internal; its internal
  # ingress admits a VM of this project's VPC, Cloud NAT egress included)
  # and the internal token Python holds, a version pinned by its number
  # (the node reads secrets that way only; Python reads the latest). The
  # token is the enclaves' credential, held in their running clients, so
  # its rotation is the control plane's procedure, not this file's: this
  # pin moves to a new version only once the callers carry it, and each
  # node is then drained and restarted in turn with its running argument
  # checked (docs/runbooks/fastpath-deploy.md). Set on the nodes'
  # metadata, read when a node's startup script runs.
  fastpath_edge_port         = 8081
  fastpath_edge_upstream     = "https://trusted-router-vsjf6qu4la-uc.a.run.app"
  fastpath_edge_token_secret = "projects/${local.gcp_project_id}/secrets/trustedrouter-internal-gateway-token/versions/1"
  # The edge's routing snapshot and its shadow comparator (E2, docs/design/
  # routing-snapshot-v1.md): every node polls Python's published snapshot
  # on the edge's own upstream (the startup script refuses any other
  # origin), verifies its manifest with version 1 of
  # trustedrouter-routing-snapshot-signing-key's public half, under its kid
  # and plane, and, shadow on, compares Go's verdict with Python's on each
  # authorize the edge passes on, logging counts alone: Python still
  # decides. A node reads these when its startup script runs, and only a
  # script from #1969 on reads them; every node runs one, 5 and 6 having
  # been added under new names for it (docs/runbooks/fastpath-deploy.md,
  # "Turning the edge on"). Until publishing is on
  # (docs/runbooks/routing-snapshot-key.md) the poll answers 404 and the
  # edge keeps polling, admission unaffected.
  # The edge policy (stage I1, docs/design/fast-admission-e2-decide.md
  # section 4): Python's signed live facts, polled on the same upstream and
  # verified with the same key, kid and plane. Only a script from I1 on
  # reads the policy's two attributes, which nodes 7 and 8 run.
  # The decide shadow (G10) is on, once the policy had been held through a
  # measured soak (the I1 runbook); each node in turn took it drained, its
  # unit stopped and its startup script run again (a reset alone keeps the
  # unit's old arguments). Python still decides.
  # The decide path (G11, stage I2): fastpath-edge-decide "true" has the
  # edge answer the authorizes of the workspaces the decide table names
  # (docs/runbooks/fastpath-edge-decide.md); "false", the default, answers
  # nothing. It is a flag the unit takes, so a node takes a change to it as
  # it took the shadow's: drained, its unit stopped and its startup script
  # run again, one node at a time. Only a script that reads the attribute
  # passes the flag: a node from before that script carries the attribute
  # and passes nothing, so the script reaches the fleet first, as I1's did,
  # through nodes added under new names: 9 and 10 beside 7 and 8, which
  # then leave the backend and are removed. On since the I3 soak was green;
  # with every decide row off it still answers nothing.
  fastpath_edge_snapshot = tomap({
    fastpath-edge-snapshot-url        = "${local.fastpath_edge_upstream}/internal/fastpath/routing-snapshot"
    fastpath-edge-snapshot-public-key = "oTQF4Efsi6G7_38Nca4HjiB7QUoNDkNSzaOw26Kt9IM"
    fastpath-edge-snapshot-kid        = "tr-routing-snapshot-2026-10a"
    fastpath-edge-snapshot-plane      = "gcp/${local.fastpath_region}"
    fastpath-edge-shadow              = "true"
    fastpath-edge-policy-url          = "${local.fastpath_edge_upstream}/internal/fastpath/edge-policy"
    fastpath-edge-decide-shadow       = "true"
    fastpath-edge-decide              = "true"
  })
  # The fleet audit (docs/design/fast-admission-widening.md section 7): true
  # gives every node fastpath-fleet-audit "true", so whichever node owns the
  # ring key fleet-audit audits every workspace the switch names, a pass an
  # hour, and turns on the policy on its summary line's absence
  # (infra/fastpath_alerts.tf). Off until its activation: a node from before
  # the startup script that reads the attribute passes nothing, so the
  # script reaches the fleet first through nodes added under new names, as
  # the decide attribute's did, and the pin must take -fleet-audit
  # (docs/runbooks/fastpath-deploy.md, "A flag the unit passes"). On for
  # WS0's gate (widening section 11 item 2, "before WS0's gate is read"):
  # nodes 13 and 14 run the reading script, bd4398c21 takes -fleet-audit, and
  # each node takes the flag by the runbook's unit rewrite, not a reset.
  fastpath_fleet_audit = true
  # The money settings (docs/design/fast-admission-widening.md (b), WS0 of
  # section 8.2): a lease needs effective trust tier 3 (1 since 2026-11-13,
  # with the lower tiers' allowances below), a tier-3 workspace's
  # allowance is $1,000 and a lease leaves at least $20 of the balance ($2
  # since 2026-11-20, at the floor below), in
  # microdollars. The startup script passes each as -required-tier,
  # -allowance and -floor (8af87998a and later take all three). A reset
  # alone keeps the unit's old arguments: each node takes them one at a
  # time, the other serving, by runbook fastpath-deploy.md's money settings
  # step 5 (leaving, its leases and the balancer drained, the unit stopped,
  # the startup script run again, the process's arguments read back), and
  # WS0's gate counts from the last node's restart. Every pilot workspace
  # passed fastpathctl ready at these values before this change but the
  # customer's, whose switch is off, so it takes today's path and cannot be
  # refused at the tier. Step 4: over the leases kept (seven days), the
  # largest lease under $4 and the lease spend far under a quarter of the
  # allowance; a lease tops up at most $10. Rollback: each value written
  # back as it was and each node taken the same way. Never remove an
  # attribute to go back: a node without one runs the release's default,
  # which for the floor is none and for the allowance bounds nothing.
  fastpath_required_tier = "1"
  fastpath_allowance     = "1000000000"
  # The floor, $2 since 2026-11-20 ($20 before; the design's (b), amended
  # that day). It is the headroom a new lease must leave outside leases for
  # the same workspace's synchronous requests, not a fraud control: the
  # required tier and the allowances are. A lease is taken whole or not at
  # all: the node's lease check refuses it unless the headroom is at least
  # the floor plus the whole amount asked, and an ask is about a minute of
  # the shard's spend, at least $0.01 and at most $10, so at $20 no balance
  # under about $20 was ever leased. Read on 2026-11-20
  # over seven days: of the workspaces at tier 1 or above left out for their
  # balance alone, 55 had traffic, about 20% of all requests, most of it
  # from balances under $5; the largest ordinary hold each could make (its
  # largest prompt and 16,384 output tokens at its models' highest credit
  # rates) was at most $0.50 for 51 of them and above $2 for one. A
  # synchronous hold above the headroom left, while leases hold the rest, is
  # answered 503 with Retry-After (TR_FASTPATH_LEASE_HEADROOM_503), which
  # the client may retry: nothing is admitted or charged wrongly.
  # Applying this changes the instances' metadata only. A node takes the
  # value when its unit starts again, one node at a time, the other
  # serving, by fastpath-deploy.md's "Lowering the floor", and until both
  # have, a lease is checked at whichever floor its owner runs. The enabler's
  # join and leave balances come down only after both nodes run it
  # (tests/test_fastpath_enabler_deploy.py holds the order). The way back
  # is "20000000" written here again, by reverting the change that lowered
  # it, after the enabler's balances are raised; never the attribute
  # removed, which would leave no floor at all.
  fastpath_floor = "2000000"
  # The lower tiers' allowances (docs/design/fast-admission-widening.md
  # (b)), in microdollars, each at most the tier above's: $250 for tier 2
  # (paid, owner identity approved) and $100 for tier 1 (paid, identity not
  # approved), Joseph's call on 2026-11-12. The exposure bound is the
  # allowance times 250 per workspace. They land with the pin of the first
  # release that takes -allowance-tier2 and -allowance-tier1 (#2542), and
  # the required tier is lowered to 1 in the same change. Only a node
  # created from the script that reads them takes them (15 and 16 below);
  # docs/runbooks/fastpath-deploy.md, "Lowering the required tier".
  # The enabler's tier is lowered after every node runs them.
  fastpath_allowance_tier2 = "250000000"
  fastpath_allowance_tier1 = "100000000"
  # The settle log's hedge (-settle-hedge, #2671): "" leaves the attribute
  # off, and a node runs its release's default (250ms); "0" turns the hedge
  # off. Set only with a pinned release that takes the flag, and only a node
  # created from the script that reads the attribute takes it, by the unit
  # rewrite in docs/runbooks/fastpath-deploy.md, "The settle log's hedge".
  fastpath_settle_hedge = ""
  fastpath_settle_hedge_metadata = {
    for name, value in { fastpath-settle-hedge = local.fastpath_settle_hedge } : name => value if value != ""
  }
  # What an owner does with a starved shard's leases (-starved-leases;
  # docs/design/billing-503-reduction-2026-11.md, option A): "" leaves the
  # attribute off, and a node runs its release's default (log: logged,
  # nothing closed); "close" turns it on, "off" silences it. Set only with
  # a pinned release that takes the flag, and only a node created from the
  # script that reads the attribute takes it, by the unit rewrite in
  # docs/runbooks/fastpath-deploy.md, "Starved leases".
  fastpath_starved_leases = ""
  fastpath_starved_leases_metadata = {
    for name, value in { fastpath-starved-leases = local.fastpath_starved_leases } : name => value if value != ""
  }
  # The decide path's read-through (-edge-decide-cache-wait,
  # docs/design/fast-admission-e2-decide.md decision 2): "" leaves the
  # attribute off, and a node runs its release's default, no wait; "100ms"
  # has a request whose cached key or credit entry is cold or stale wait,
  # on a timer of that length started after its first look, for the strong
  # read that fills it, rather than pass as cache.key. Set only with a
  # pinned release that takes the flag, and only a node created from the
  # script that reads the attribute takes it, by the unit rewrite in
  # docs/runbooks/fastpath-deploy.md, "The decide cache's wait". A node's
  # own value, its for_each entry's cache_wait below, takes this one's place
  # on that node alone; "" there leaves this one. So "0", or "" again, here
  # turns the wait off only where no node carries its own: the off switch is
  # every cache_wait "" (or "0") as well, and before pinning a release
  # without the flag, every value "", since "0" still passes it.
  fastpath_edge_decide_cache_wait = ""
  fastpath_edge_decide_cache_wait_metadata = {
    for name, value in { fastpath-edge-decide-cache-wait = local.fastpath_edge_decide_cache_wait } : name => value if value != ""
  }
}

# --- The topics and the auditor's subscriptions ---------------------------

# The settle log: an owner's records for a lease, in order on the lease's
# ordering key, published through the region's locational endpoint, so the
# topic stores its messages in that region.
resource "google_pubsub_topic" "fastpath_settle_log" {
  project                    = local.gcp_project_id
  name                       = "settle-log"
  message_retention_duration = "604800s"

  message_storage_policy {
    allowed_persistence_regions = [local.fastpath_region]
  }
}

# The record topic: each request's full record and each winner's outcome,
# unordered.
resource "google_pubsub_topic" "fastpath_records" {
  project                    = local.gcp_project_id
  name                       = "records"
  message_retention_duration = "604800s"

  message_storage_policy {
    allowed_persistence_regions = [local.fastpath_region]
  }
}

# The auditor's ordered subscription to the settle log. The ack deadline is
# the one the service's tests give it; the client extends it while a
# message is in hand.
resource "google_pubsub_subscription" "fastpath_auditor" {
  project                    = local.gcp_project_id
  name                       = "auditor"
  topic                      = google_pubsub_topic.fastpath_settle_log.id
  enable_message_ordering    = true
  ack_deadline_seconds       = 10
  message_retention_duration = "604800s"

  expiration_policy {
    ttl = "" # never expires for want of subscribers
  }
}

# The record stager's subscription to the record topic.
resource "google_pubsub_subscription" "fastpath_stager" {
  project                    = local.gcp_project_id
  name                       = "stager"
  topic                      = google_pubsub_topic.fastpath_records.id
  ack_deadline_seconds       = 10
  message_retention_duration = "604800s"

  expiration_policy {
    ttl = ""
  }
}

# --- The archives (W7): every message of each topic, with its attributes
# --- and metadata, kept; the only way back for a lease stopped at a gap.

resource "google_storage_bucket" "fastpath_archive" {
  for_each = toset(["settle-log", "records"])

  project                     = local.gcp_project_id
  name                        = "tr-fastpath-archive-${each.key}-${local.gcp_project_id}"
  location                    = upper(local.fastpath_region)
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  lifecycle {
    prevent_destroy = true
  }
}

# Pub/Sub's service agent writes the archives.
resource "google_storage_bucket_iam_member" "fastpath_archive_writer" {
  for_each = google_storage_bucket.fastpath_archive

  bucket = each.value.name
  role   = "roles/storage.admin"
  member = local.fastpath_pubsub_agent
}

resource "google_pubsub_subscription" "fastpath_archive" {
  for_each = {
    "settle-log" = google_pubsub_topic.fastpath_settle_log.id
    "records"    = google_pubsub_topic.fastpath_records.id
  }

  project = local.gcp_project_id
  name    = "${each.key}-archive"
  topic   = each.value
  # A Cloud Storage subscription's max_duration (300 s) must not exceed its
  # deadline, and the live subscriptions report 300: so says this, and a
  # plan is clean.
  ack_deadline_seconds       = 300
  message_retention_duration = "604800s"

  expiration_policy {
    ttl = ""
  }

  cloud_storage_config {
    bucket          = google_storage_bucket.fastpath_archive[each.key].name
    filename_prefix = "${each.key}/"
    max_duration    = "300s"
    max_bytes       = 100000000

    avro_config {
      write_metadata = true
    }
  }

  depends_on = [google_storage_bucket_iam_member.fastpath_archive_writer]
}

# --- The nodes' identity, the key's secret, and the releases ---------------

resource "google_service_account" "fastpath_node" {
  project      = local.gcp_project_id
  account_id   = "tr-fastpath-node"
  display_name = "TrustedRouter fast admission node"
}

# The envelope key: a version is added by hand, pinned by number in the
# nodes' -key-secret (docs/runbooks/fastpath-key-rotation.md).
resource "google_secret_manager_secret" "fastpath_envelope_key" {
  project   = local.gcp_project_id
  secret_id = "fastpath-envelope-key"

  replication {
    auto {}
  }
}

# The internal token the edge checks, the same secret the control plane
# holds (scripts/deploy/secrets.sh creates it; it is not managed here).
resource "google_secret_manager_secret_iam_member" "fastpath_node_gateway_token" {
  project   = local.gcp_project_id
  secret_id = "trustedrouter-internal-gateway-token"
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.fastpath_node.member
}

resource "google_secret_manager_secret_iam_member" "fastpath_node_key" {
  project   = local.gcp_project_id
  secret_id = google_secret_manager_secret.fastpath_envelope_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.fastpath_node.member
}

resource "google_pubsub_topic_iam_member" "fastpath_node_publisher" {
  for_each = {
    "settle-log" = google_pubsub_topic.fastpath_settle_log.name
    "records"    = google_pubsub_topic.fastpath_records.name
  }

  project = local.gcp_project_id
  topic   = each.value
  role    = "roles/pubsub.publisher"
  member  = google_service_account.fastpath_node.member
}

resource "google_pubsub_subscription_iam_member" "fastpath_node_subscriber" {
  for_each = {
    "auditor" = google_pubsub_subscription.fastpath_auditor.name
    "stager"  = google_pubsub_subscription.fastpath_stager.name
  }

  project      = local.gcp_project_id
  subscription = each.value
  role         = "roles/pubsub.subscriber"
  member       = google_service_account.fastpath_node.member
}

resource "google_spanner_database_iam_member" "fastpath_node_database" {
  project  = local.gcp_project_id
  instance = google_spanner_instance.trusted_router.name
  database = "trusted-router"
  role     = "roles/spanner.databaseUser"
  member   = google_service_account.fastpath_node.member
}

# The nodes' project-level roles, roles/logging.logWriter and
# roles/monitoring.metricWriter, are granted by hand (docs/runbooks/
# fastpath-p0.md): the deploy identity that applies this does not edit the
# project's IAM policy, by design.

# The releases: fastpath-release.yml uploads each CI-passed main commit's
# binaries under fastpath/<commit>/; the nodes read the one named above.
# The workflow never overwrites a published object (it uploads with
# --if-generation-match=0), and the nodes check what they download against
# the digests pinned above, but anything holding the bucket's admin role can
# still overwrite or delete an object. So the bucket keeps every object's
# earlier generations: with versioning, an overwrite or a delete leaves the
# object before it as a noncurrent version, readable by its generation,
# for a year. Every live release is kept, whatever its age: any of them may
# be pinned again, a rollback included, and a release a node is pinned to
# must stay readable for the node's next reset. Pruning releases no pin can
# name is a separate decision (a release is about 140 MB).
resource "google_storage_bucket" "fastpath_releases" {
  project                     = local.gcp_project_id
  name                        = local.fastpath_release_bucket
  location                    = upper(local.fastpath_region)
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      with_state                 = "ARCHIVED"
      days_since_noncurrent_time = 365
    }
    action {
      type = "Delete"
    }
  }
}

resource "google_storage_bucket_iam_member" "fastpath_releases_writer" {
  bucket = google_storage_bucket.fastpath_releases.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:tr-deploy@${local.gcp_project_id}.iam.gserviceaccount.com"
}

resource "google_storage_bucket_iam_member" "fastpath_releases_reader" {
  bucket = google_storage_bucket.fastpath_releases.name
  role   = "roles/storage.objectViewer"
  member = google_service_account.fastpath_node.member
}

# --- The nodes ---------------------------------------------------------------

# A node in each of two zones, on the default network's us-central1 subnet
# behind the project's NAT (tr-nat), no external address; the network's
# default-allow-internal rule lets the fleet reach port 8080. A node is
# replaced, never restarted in place, once it holds leases
# (docs/runbooks/fastpath-deploy.md); at P0 it holds none. Nothing counts
# the nodes: front doors find owners and peers in the membership table,
# auditor members share the subscriptions, and the edge's firewall rule
# names the nodes' service account.
resource "google_compute_instance" "fastpath_node" {
  # 3 and 4 took the places of 1 and 2 on 2026-10-20, with the edge on
  # (docs/runbooks/fastpath-deploy.md, "Turning the edge on"); 5 and 6 took
  # the places of 3 and 4 the same way, with the routing snapshot and the
  # shadow on (local.fastpath_edge_snapshot), which the script 3 and 4 ran,
  # from before #1969, read neither of. Each replaced pair was marked
  # leaving and drained, left the edge backend (local.fastpath_edge_retiring)
  # one apply before its removal, then was removed. 7 and 8 took the places
  # of 5 and 6 the same way, with the edge policy's attributes (stage I1),
  # which the script 5 and 6 ran, from before I1, read neither of. 9 and 10
  # took the places of 7 and 8 the same way, with the decide attribute
  # (stage I2, "false" until the I2 switch), which the script 7 and 8 ran,
  # from before I2, did not read; 7 and 8 were drained, left the edge
  # backend one apply before their removal, then were removed.
  #
  # 11 and 12 took the places of 9 and 10 the same way, on four cores rather
  # than two: at 1,000 generations a second (2026-11-04, P1's runs on
  # tr-fastpath-loadgen) the two-core nodes reached 77% CPU with the
  # authorize p99 at 64 ms; on four cores five minutes at that rate held,
  # the p99 at 27 ms and every audit clean. The same script, release and
  # metadata; 9 and 10 were drained, left the edge backend one apply before
  # their removal, then were removed.
  #
  # 13 and 14 took the places of 11 and 12 the same way, for the money
  # settings (#2354): the script 11 and 12 ran, from before it, passed none
  # of -required-tier, -allowance or -floor from their attributes. The same
  # release and metadata; 11 and 12 were drained, left the edge backend one
  # apply before their removal, then were removed.
  #
  # 15 and 16 took the places of 13 and 14 the same way, for the lower
  # tiers' allowances (#2542): the script 13 and 14 ran, from before it,
  # passed neither -allowance-tier2 nor -allowance-tier1 from their
  # attributes. The same release and metadata; 13 and 14 were drained, left
  # the edge backend one apply before their removal, then were removed.
  #
  # 17 and 18 take the places of 15 and 16 the same way, for the settle
  # log's hedge (#2671), starved leases (#2906) and the decide cache's wait
  # (#2903): the script 15 and 16 run, from before them, passes none of
  # -settle-hedge, -starved-leases or -edge-decide-cache-wait from their
  # attributes, and checks no release against its pinned digests (#2668).
  # The same release and metadata; 15 and 16 are drained, leave the edge
  # backend one apply before their removal, then are removed.
  #
  # cache_wait is the node's own fastpath-edge-decide-cache-wait: "" adds
  # nothing, so the node carries local.fastpath_edge_decide_cache_wait's
  # value or none; "0" or 1ms to 250ms is the node's alone and takes the
  # fleet value's place. Only a node from 17 on runs the script that reads
  # the attribute, so only such a node carries one, and the decide path must
  # be on (tests/test_fastpath_edge_decide_cache_wait.py). Every entry names
  # cache_wait, so the entries share one shape. 17 carries 100ms, the canary
  # of docs/runbooks/fastpath-deploy.md, "The decide cache's wait" step 2,
  # read beside 18 for an hour.
  for_each = {
    "tr-fastpath-15" = { zone = "us-central1-a", machine_type = "e2-standard-4", cache_wait = "" }
    "tr-fastpath-16" = { zone = "us-central1-b", machine_type = "e2-standard-4", cache_wait = "" }
    "tr-fastpath-17" = { zone = "us-central1-a", machine_type = "e2-standard-4", cache_wait = "100ms" }
    "tr-fastpath-18" = { zone = "us-central1-b", machine_type = "e2-standard-4", cache_wait = "" }
  }

  project      = local.gcp_project_id
  name         = each.key
  zone         = each.value.zone
  machine_type = each.value.machine_type

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
      size  = 20
    }
  }

  network_interface {
    network    = "default"
    subnetwork = "default"
    # No access_config: no external address.
  }

  service_account {
    email  = google_service_account.fastpath_node.email
    scopes = ["cloud-platform"]
  }

  # The node's own cache_wait comes after the fleet's decide cache wait, so
  # it takes that value's place; "" adds nothing.
  metadata = merge({
    enable-oslogin               = "TRUE"
    fastpath-release             = local.fastpath_release
    fastpath-release-sha256      = local.fastpath_release_sha256
    fastpath-release-sums-sha256 = local.fastpath_release_sums_sha256
    fastpath-edge-upstream       = local.fastpath_edge_upstream
    fastpath-edge-token-secret   = local.fastpath_edge_token_secret
    fastpath-fleet-audit         = local.fastpath_fleet_audit ? "true" : "false"
    fastpath-required-tier       = local.fastpath_required_tier
    fastpath-allowance           = local.fastpath_allowance
    fastpath-floor               = local.fastpath_floor
    fastpath-allowance-tier2     = local.fastpath_allowance_tier2
    fastpath-allowance-tier1     = local.fastpath_allowance_tier1
  }, local.fastpath_edge_snapshot, local.fastpath_starved_leases_metadata, local.fastpath_edge_decide_cache_wait_metadata, { for name, value in { fastpath-edge-decide-cache-wait = each.value.cache_wait } : name => value if value != "" }, local.fastpath_settle_hedge_metadata)

  metadata_startup_script = templatefile("${path.module}/fastpath_node_startup.sh.tftpl", {
    release_bucket = google_storage_bucket.fastpath_releases.name
    region         = local.fastpath_region
    database       = local.fastpath_database
    project        = local.gcp_project_id
    key_secret     = "projects/${local.gcp_project_id}/secrets/${google_secret_manager_secret.fastpath_envelope_key.secret_id}/versions/1"
    edge_port      = local.fastpath_edge_port
  })

  labels = {
    service = "fastpath"
    stage   = "p0"
  }

  allow_stopping_for_update = true

  lifecycle {
    # A node is replaced, never restarted in place, and the new one starts
    # first (docs/runbooks/fastpath-deploy.md): a startup-script change
    # reaches the fleet through nodes added under new names and the old
    # ones drained and removed, not through a same-name replacement that
    # takes both down at once. A node keeps the script it was created
    # with; the pin, the edge's attributes and the snapshot's are metadata,
    # applied in place, read when the script next runs.
    # The cost: a script change is not drift here, so a node created
    # before it runs the old script while the plan is clean and its
    # binary matches the pin; which script a node runs is read from the
    # instance (docs/runbooks/fastpath-deploy.md, "Turning the edge on").
    ignore_changes = [metadata_startup_script]

    # A node whose script verifies its release refuses a release without
    # its pinned digests, so a pin lands with them or not at all.
    precondition {
      condition     = local.fastpath_release_digests_ok
      error_message = "local.fastpath_release_digests has no well-formed fastpath and sha256sums digests for local.fastpath_release; copy them from the release's published SHA256SUMS (docs/runbooks/fastpath-deploy.md, \"Pinning a release\")."
    }
  }

  depends_on = [
    google_spanner_database_iam_member.fastpath_node_database,
    google_pubsub_subscription_iam_member.fastpath_node_subscriber,
    google_storage_bucket_iam_member.fastpath_releases_reader,
  ]
}

# --- The load generator ------------------------------------------------------

# One machine of its own for the edge's 1,000 a second run, so the run does
# not measure a generator beside the front door and owner it drives (P1's
# results, "Next"). It runs no node unit and joins no membership: its script
# only installs the pinned release's loadgen, fastpathwatch and load mix, and
# the run is started by hand over OS Login (docs/runbooks/fastpath-p1.md).
# It takes the nodes' service account for the release bucket and the
# readiness read loadgen makes, and the default-allow-internal rule for the
# front doors' port. Off is local.fastpath_loadgen = false, which removes it.
locals {
  fastpath_loadgen = true
}

resource "google_compute_instance" "fastpath_loadgen" {
  count = local.fastpath_loadgen ? 1 : 0

  project      = local.gcp_project_id
  name         = "tr-fastpath-loadgen"
  zone         = "us-central1-c"
  machine_type = "e2-standard-8"

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
      size  = 20
    }
  }

  network_interface {
    network    = "default"
    subnetwork = "default"
    # No access_config: no external address.
  }

  service_account {
    email  = google_service_account.fastpath_node.email
    scopes = ["cloud-platform"]
  }

  metadata = {
    enable-oslogin               = "TRUE"
    fastpath-release             = local.fastpath_release
    fastpath-release-sums-sha256 = local.fastpath_release_sums_sha256
  }

  # The tools are verified as a node verifies its release
  # (infra/fastpath_node_startup.sh.tftpl): SHA256SUMS against the digest
  # pinned beside the release, each tool against SHA256SUMS, all in a
  # staging directory before any is installed; any failure installs nothing.
  metadata_startup_script = <<-EOT
    #!/bin/bash
    set -euo pipefail
    attribute() { curl -sf -H 'Metadata-Flavor: Google' "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1"; }
    release="$(attribute fastpath-release)"
    sums_sha256="$(attribute fastpath-release-sums-sha256 || true)"
    refuse() {
      echo "fastpath-loadgen: release $release failed verification: $1; nothing installed" | systemd-cat -t fastpath-loadgen -p err
      exit 1
    }
    [[ $release =~ ^[0-9a-f]{40}$ ]] || refuse "fastpath-release is not a 40-character commit"
    [[ $sums_sha256 =~ ^[0-9a-f]{64}$ ]] || refuse "fastpath-release-sums-sha256 is not a pinned sha256 digest"
    install -d /opt/fastpath
    rm -rf /opt/fastpath/.stage.*
    stage="$(mktemp -d /opt/fastpath/.stage.XXXXXX)"
    trap 'rm -rf "$stage"' EXIT
    gcloud storage cp "gs://${google_storage_bucket.fastpath_releases.name}/fastpath/$release/SHA256SUMS" "$stage/SHA256SUMS" \
      || refuse "its SHA256SUMS could not be read"
    sums_got="$(sha256sum < "$stage/SHA256SUMS" | cut -d' ' -f1)"
    [ "$sums_got" = "$sums_sha256" ] || refuse "its SHA256SUMS has sha256 $sums_got, not the pinned $sums_sha256"
    for tool in loadgen fastpathwatch load-mix.json; do
      want="$(awk -v name="$tool" '$2 == name { print $1 }' "$stage/SHA256SUMS")"
      [[ $want =~ ^[0-9a-f]{64}$ ]] || refuse "its SHA256SUMS does not list one sha256 digest for $tool"
      gcloud storage cp "gs://${google_storage_bucket.fastpath_releases.name}/fastpath/$release/$tool" "$stage/$tool" \
        || refuse "$tool could not be read"
      got="$(sha256sum < "$stage/$tool" | cut -d' ' -f1)"
      [ "$got" = "$want" ] || refuse "$tool has sha256 $got, not SHA256SUMS's $want"
    done
    for tool in loadgen fastpathwatch load-mix.json; do
      chmod 0755 "$stage/$tool"
      mv "$stage/$tool" "/opt/fastpath/$tool"
    done
    echo "$release" > /opt/fastpath/release
  EOT

  labels = {
    service = "fastpath"
    stage   = "loadgen"
  }

  allow_stopping_for_update = true

  lifecycle {
    # metadata_startup_script forces a new instance when it changes, and
    # infra-apply refuses any plan that replaces a resource
    # (scripts/deploy/check_terraform_plan.py), so the generator, like the
    # nodes, keeps the script it was created with: the verifying script
    # above reaches it when it is next created (replaced by an owner's
    # `terraform apply -replace`, or deleted by hand and recreated by the
    # next apply). Its pin and digest are metadata, applied in place.
    ignore_changes = [metadata_startup_script]

    precondition {
      condition     = local.fastpath_release_digests_ok
      error_message = "local.fastpath_release_digests has no well-formed fastpath and sha256sums digests for local.fastpath_release; copy them from the release's published SHA256SUMS (docs/runbooks/fastpath-deploy.md, \"Pinning a release\")."
    }
  }

  depends_on = [
    google_storage_bucket_iam_member.fastpath_releases_reader,
  ]
}
