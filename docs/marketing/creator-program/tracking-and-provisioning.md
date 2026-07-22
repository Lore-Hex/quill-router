# Creator tracking and provisioning

## Link convention

Every placement gets one signed first-party attribution path:

```text
https://trustedrouter.com/for-developers
  ?utm_source=creator
  &utm_medium=sponsorship
  &utm_campaign=creator_pilot_202607
  &utm_content=<creator>_<concept>
  &utm_term=<viewer_code>
```

The manifest contains unique creator slugs, concepts, and viewer codes. Viewer
codes are attribution labels in this pilot. They do not redeem public credit and
must not be advertised as coupons unless a separate redemption system is
approved and tested.

The primary conversion is `first_successful_api_call`. Also report sign-in
opened, signup, key creation, seven-day retained API use, credit purchase, and
30-day gross margin.

## Provisioning safety

Creator workspaces stay owned by the TrustedRouter operator until a partner is
contracted. Each creator receives only a non-management inference key with:

- a fixed lifetime spend limit equal to the funded credit;
- an enforced daily limit;
- a 90-day expiration;
- campaign, creator, and purpose tags;
- an idempotent funding event; and
- no ability to list keys, mutate workspaces, configure BYOK, or call internal
  management endpoints.

Dry-run one creator:

```bash
uv run python scripts/provision_creator_pilot.py \
  --owner-email joseph@jperla.com \
  --creator theo_t3gg
```

Apply after the agreement is signed:

```bash
uv run python scripts/provision_creator_pilot.py \
  --owner-email joseph@jperla.com \
  --creator theo_t3gg \
  --secrets-file /Users/jperla/claude/.trustedrouter_creator_pilot.private \
  --apply
```

The private file must remain mode 0600 and outside Git. The script persists a
recoverable raw key before committing its one-way hash, never prints raw keys,
and reuses deterministic funding event IDs so retries cannot grant credit
twice.

Provision only accepted creators. Running the command without `--creator`
targets every manifest entry and should be reserved for an explicitly approved
batch.
