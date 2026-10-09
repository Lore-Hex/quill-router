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

## A rotation is three deploys

OLD is the version in use and NEW the one replacing it.

1. Add NEW, 32 random bytes, without writing them to a file:

   ```bash
   head -c 32 /dev/urandom | gcloud secrets versions add S --data-file=-
   ```

2. Deploy every node with `-key-secret OLD -accept-key-secrets NEW`. Wait
   until the deploy has replaced every node, so no node refuses NEW.
3. Deploy every node with `-key-secret NEW -accept-key-secrets OLD`. Wait
   until the deploy has replaced every node, and note when the last node
   sealing with OLD stopped.
4. Keep OLD accepted for 55 hours after that: a hold sealed with OLD lives
   up to 2 hours 20 minutes, and the enclave's queue retries its settle for
   up to about 52 hours (design §4.8). Only then deploy every node with
   `-key-secret NEW` and no accepted key.
5. Once that deploy has replaced every node, disable OLD's version. Destroy
   it only when no rollback would deploy it again.

Each deploy keeps its leases as W8's deploys do. Rolling a step back is
deploying the step before it, so every version a step names stays enabled
until the step after it has replaced every node.

A rotation that starts within 55 hours of the last one keeps both older
keys accepted: `-accept-key-secrets OLDER,OLD`, until each one's 55 hours
have passed.
