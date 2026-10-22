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
   Two conditions come first.

   The catalog must be rebuilt as of `at`, both endpoints and models
   (`docs/design/routing-snapshot-v1.md`, "Not yet"). Until then, at every
   retirement (00:00 UTC) each published copy expires and every instance
   imported before the retirement refuses until a deploy replaces it, hours
   with no snapshot. Replacing every instance at each retirement is a weaker
   substitute (it depends on a restart timed to every retirement, and leaves
   minutes of gap while revisions roll); do not enable on it alone.

   And the compile must be measured where it will run. It is 1.5 to 4.5 s of
   CPU-bound Python on development machines (4.3 s measured in review), and
   on the combined service it holds the GIL against that instance's authorize
   requests for as long, once a window on each instance the edge polls.
   Before this step, time `compile_routing_snapshot` with
   `CatalogInputs.live` on the release image and the service's CPU shape
   (for example a one-off Cloud Run job of that image), and decide the
   authorize latency it implies is acceptable. After it, watch the
   `routing snapshot compiled` log line's `elapsed_ms` and authorize latency
   on the polled instances; if it hurts authorize, turn publishing off.

6. Check it where the edge will poll it: the us-central1 service's run.app
   address, whose ingress admits only the project's network and the load
   balancer (`trustedrouter.com/internal/*` goes to the internal surface,
   which does not publish). On a fast path node, whose identity reads the
   internal token:

   ```bash
   gcloud compute ssh tr-fastpath-3 --zone us-central1-a --project quill-cloud-proxy \
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
   retries; an instance started before a retirement refuses every later
   window until it is replaced, and the edge's copy expires at that
   retirement, so nothing is published meanwhile), or `snapshot-error` (any other failure,
   logged by its class as `routing snapshot compile failed`, retried after
   5 s).

## Turning it off

Set `ROUTING_SNAPSHOT_PUBLISH_ENABLED=false` and deploy: the route is gone
(404) and the edge keeps its last good copy until that copy's `not_after`,
then passes every request to Python. The mount can stay.

## Rotating

A rotation adds version N+1 and moves the edge before the control plane, so
no edge ever refuses the key a snapshot is signed with:

1. Add the new version as in steps 2 and 3, under a new kid
   (`tr-routing-snapshot-YYYY-MMx`).
2. Give the edge both public keys (its configuration arrives with the edge's
   poll, a later change), and wait until every node runs with both.
3. In `rollout.sh`, set the version, the public key and the kid to the new
   ones, and deploy.
4. Once six hours have passed since the deploy finished (the longest window a
   snapshot signed with the old key is valid for), remove the old public key
   from the edge, then disable the old version. Destroy it only when no
   configuration would name it again.
