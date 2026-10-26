# The routing snapshot's signing key

The control plane signs each routing snapshot's manifest with an Ed25519 key
(`docs/design/routing-snapshot-v1.md`, "Publishing"); the fast path's edge
will verify it by the key's public half. The private key lives in the Secret
Manager secret `trustedrouter-routing-snapshot-signing-key`
(`infra/routing_snapshot_key.tf`), readable only by the control plane's
runtime identity, `trusted-router-control-run@`. The control plane mounts one
version of it, pinned by its number, never `latest`
(`scripts/deploy/rollout.sh`), so every instance and every restart signs with
the same key.

Publishing ships dark: the secret has no version, `rollout.sh` mounts nothing
and renders `TR_ROUTING_SNAPSHOT_PUBLISH_ENABLED=false`, and the route
`/internal/fastpath/routing-snapshot` does not exist (404).

## The literals in rollout.sh

| Literal | Meaning |
|---|---|
| `ROUTING_SNAPSHOT_PUBLISH_ENABLED` | `true` or `false`: whether the route is registered |
| `ROUTING_SNAPSHOT_KEY_VERSION` | the secret version mounted at `/secrets/routing-snapshot/signing.pem`; empty mounts nothing |
| `ROUTING_SNAPSHOT_PUBLIC_KEY` | that version's public key, base64url without padding (43 characters) |
| `ROUTING_SNAPSHOT_KID` | the key's name in the manifest header, `tr-routing-snapshot-2026-10a` |

The rollout refuses `ROUTING_SNAPSHOT_PUBLISH_ENABLED=true` without a version
number and a public key, and any version that is not a number. The publisher
refuses a mounted key whose public half is not `ROUTING_SNAPSHOT_PUBLIC_KEY`,
and a key file or kid another signing purpose of the process uses; the route
then answers 503 `{"status": "signer-unavailable"}`.

## Adding version 1 and turning publishing on

1. Check the control plane's identity is the secret's one reader:

   ```bash
   gcloud run services describe trusted-router --region us-central1 \
     --project quill-cloud-proxy --format='value(spec.template.spec.serviceAccountName)'
   ```

   It must print `trusted-router-control-run@quill-cloud-proxy.iam.gserviceaccount.com`,
   the member `infra/routing_snapshot_key.tf` grants. If it prints another
   account, change the Terraform member first.

2. Generate the key straight into Secret Manager, without writing it to a
   file:

   ```bash
   openssl genpkey -algorithm ed25519 | gcloud secrets versions add \
     trustedrouter-routing-snapshot-signing-key --project quill-cloud-proxy --data-file=-
   ```

   It prints the version it added: `1`.

3. Derive its public key, which prints only the public half:

   ```bash
   gcloud secrets versions access 1 --secret trustedrouter-routing-snapshot-signing-key \
     --project quill-cloud-proxy \
     | openssl pkey -pubout -outform DER \
     | python3 -c 'import base64,sys; print(base64.urlsafe_b64encode(sys.stdin.buffer.read()[-32:]).rstrip(b"=").decode())'
   ```

4. Mount it, still off. In `scripts/deploy/rollout.sh` set
   `ROUTING_SNAPSHOT_KEY_VERSION="1"` and `ROUTING_SNAPSHOT_PUBLIC_KEY` to the
   43 characters from step 3; leave `ROUTING_SNAPSHOT_PUBLISH_ENABLED=false`.
   Merge and let the deploy roll out. Every revision now mounts the key and
   nothing reads it; a missing or unreadable version fails the deploy here,
   before anything depends on it.

5. Turn it on: set `ROUTING_SNAPSHOT_PUBLISH_ENABLED=true`, merge, deploy.
   Measure first, where it will run. The publisher compiles each window in a
   worker process (`python -P -m trusted_router.routing_snapshot_worker`) as
   of the window's start, so warm and cold instances publish the same bytes
   across retirements; the worker also signs, with the mounted key file. On
   development machines it takes about 3 s of CPU (3 to 6 s under load) and
   peaks between 150 and 300 MB; the serving process itself spends about
   0.03 s checking the manifest. The worker shares the instance with the
   serving process: its CPU competes with authorize for the service's one
   vCPU (`rollout.sh` passes no `--cpu`, so Cloud Run's default of 1
   applies), once a window on each instance the edge polls, and its memory
   sits beside the serving process's within the instance's 2 GiB
   (`TR_CLOUD_RUN_MEMORY`, default `2Gi` in `scripts/deploy/_lib.sh`).
   Before this step, run the worker on the release image and the service's
   CPU and memory shape (for example a one-off Cloud Run job of that image
   with the service's environment, feeding it one request on stdin), and
   measure both: its time and peak memory, and authorize latency on an
   instance while it runs (CPU contention). If authorize suffers or memory is
   short, give the service more CPU (`--cpu`) or memory first. After this
   step, watch the `routing snapshot compiled` log line's `elapsed_ms`, the
   instances' CPU and memory, and authorize latency on the polled instances;
   if any suffers, turn publishing off.

6. Check it where the edge will poll it: the us-central1 service's run.app
   address, whose ingress admits only the project's network and the load
   balancer. The load balancer also reaches this route: no region deploys a
   separate internal surface, so `trustedrouter.com/internal/*` goes to the
   combined service in every region, behind the internal gateway token. A
   poll there compiles on that region's instance (at most once per window per
   instance), so only the edge should poll, and only here. On a fast path
   node, whose identity reads the internal token:

   ```bash
   gcloud compute ssh tr-fastpath-7 --zone us-central1-a --project quill-cloud-proxy \
     --tunnel-through-iap < check-snapshot.sh
   ```

   with `check-snapshot.sh`:

   ```bash
   TOKEN="$(gcloud secrets versions access 1 --secret trustedrouter-internal-gateway-token)"
   curl -sS -H "Authorization: Bearer ${TOKEN}" -o /tmp/snapshot.json -w '%{http_code}\n' \
     https://trusted-router-vsjf6qu4la-uc.a.run.app/internal/fastpath/routing-snapshot
   ```

   then copy `/tmp/snapshot.json` back (`gcloud compute scp ... --tunnel-through-iap`)
   and verify it in a checkout:

   ```bash
   uv run python - <<'PY'
   import base64, json, time
   from trusted_router.detached_jws import TrustedKey
   from trusted_router.routing_snapshot import Plane
   from trusted_router.routing_snapshot_signing import PURPOSE, verify_manifest
   PUBLIC = "..."  # ROUTING_SNAPSHOT_PUBLIC_KEY
   reply = json.load(open("snapshot.json"))
   body = base64.b64decode(reply["body"], validate=True)
   key = TrustedKey("tr-routing-snapshot-2026-10a", PURPOSE, PUBLIC,
                    "https://trustedrouter.com", "tr-fastpath-edge")
   print(verify_manifest(reply["manifest"], [key], body, now=int(time.time()),
                         expected_plane=Plane(cloud="gcp", region="us-central1"),
                         expected_aud="tr-fastpath-edge"))
   PY
   ```

   A 200 whose manifest verifies is published. A 503 says why in `status`:
   `signer-unavailable` (step 3's public key is not the mounted version's),
   `snapshot-refused` (the catalog refused the window; the log line
   `routing snapshot compile refused` names the reason, and the next window
   retries), `compiling` (another poll's compile is in flight; retry in 2 s),
   or `snapshot-error` (any other failure, the worker's exit or timeout
   included, logged as `routing snapshot compile failed: <class>`, the
   worker's own class prefixed `worker:`; retried after a backoff that
   starts at 5 s and doubles, up to the window's end).

## Turning it off

Set `ROUTING_SNAPSHOT_PUBLISH_ENABLED=false` and deploy: the route is gone
(404) and the edge keeps its last good copy until that copy's `not_after`,
then passes every request to Python. The mount can stay.

The edge policy (`EDGE_POLICY_PUBLISH_ENABLED` in `rollout.sh`, E2 stage I1)
is signed with this key under its own `typ`, so the rotation above moves it
too, and removing the mount needs it off first: `rollout.sh` refuses edge
policy publishing without the key. Off, its route is gone (404) and the edge
keeps its last policy until that copy's `not_after` (60 s), then passes every
request to Python.

## Rotating

A rotation adds version N+1 and moves the edge before the control plane, so
no edge ever refuses the key a snapshot is signed with:

1. Add the new version as in steps 2 and 3, under a new kid
   (`tr-routing-snapshot-YYYY-MMx`).
2. Give the edge both public keys: the nodes' `fastpath-edge-snapshot-public-key`
   and `fastpath-edge-snapshot-kid` attributes each name both, comma-separated
   in the same order (`-edge-snapshot-public-key`, `-edge-snapshot-kid`;
   `docs/design/routing-snapshot-v1.md`, "The edge's poll and shadow
   comparator"), and wait until every node runs with both.
3. In `rollout.sh`, set the version, the public key and the kid to the new
   ones, and deploy.
4. Once six hours have passed since the deploy finished (the longest window a
   snapshot signed with the old key is valid for), remove the old public key
   from the edge, then disable the old version. Destroy it only when no
   configuration would name it again.
