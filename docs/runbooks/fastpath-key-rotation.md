# Rotating the fast path's envelope key

Each admission's envelope is sealed with the fleet's key. A node verifies an
envelope with that key or any key it accepts beside it
(`docs/design/fast-admission-production-rollout.md`, W6). A node reads its
keys from Secret Manager when it starts, each from a version pinned by its
number, never `latest`, so every node and every restart reads the same key.

## The flags

- `-key-secret projects/P/secrets/S/versions/N`: the key the node seals
  with.
- `-accept-key-secrets projects/P/secrets/S/versions/M,...`: the keys it
  also verifies with. It needs `-key-secret`.
- `-key FILE`: development only. It takes no accepted keys.

A key is the secret version's raw bytes, at least 32 of them. The node's
identity needs `roles/secretmanager.secretAccessor` on the secret.

## The rule: a key stays accepted 55 hours after it last sealed

A node accepts every key that any node has sealed with in the last 55
hours, its own included. A hold sealed with a key lives up to 2 hours 20
minutes, and the enclave's queue retries its settle for up to about 52 hours
(design §4.8), so an envelope sealed with a key can come back that long
after the last node sealing with it stopped. Keep, for each key, the time
the last node sealing with it stopped. Every deploy below, a rollback
included, accepts every key whose 55 hours have not passed.

## A rotation is three deploys

OLD is the key in use and NEW the one replacing it. EARLIER are the keys
whose 55 hours have not passed, from rotations before, if any.

1. Add NEW, 32 random bytes, without writing them to a file:

   ```bash
   head -c 32 /dev/urandom | gcloud secrets versions add S --data-file=-
   ```

2. Prepare: deploy every node with `-key-secret OLD -accept-key-secrets
   NEW,EARLIER`. Wait until the deploy has replaced every node, so no node
   refuses NEW.
3. Switch: deploy every node with `-key-secret NEW -accept-key-secrets
   OLD,EARLIER`. Wait until the deploy has replaced every node, and note
   when the last node sealing with OLD stopped: OLD's 55 hours start then.
4. Retire: once a key's 55 hours have passed, deploy every node without it
   in `-accept-key-secrets`, and once that deploy has replaced every node,
   disable its version. Destroy a version only when no deploy would name it
   again.

## Rolling back

A rollback keeps both rules. Its nodes accept every key whose 55 hours have
not passed. And no node seals with a key until every node accepts it, which
is why a rotation prepares before it switches.

- Rolling back the switch while every node still accepts OLD, within OLD's
  55 hours, is one deploy: `-key-secret OLD -accept-key-secrets
  NEW,EARLIER`. NEW, having sealed, stays accepted for 55 hours after the
  rollback stopped its last sealer, as OLD did.
- Rolling back to a key some node no longer accepts, one retired, is a
  rotation to it: first prepare, every node sealing with NEW and accepting
  OLD (`-key-secret NEW -accept-key-secrets OLD,EARLIER`), and once that
  deploy has replaced every node, switch.

A node reads every version its flags name as it starts, and a disabled one
stops it starting. Before any deploy that names a disabled version, enable
it again and check that it reads:

```bash
gcloud secrets versions access N --secret S > /dev/null && echo readable
```

Each deploy keeps its leases as W8's deploys do.
