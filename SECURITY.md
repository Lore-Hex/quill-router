# Security Policy

## Reporting a vulnerability

Email **security@trustedrouter.com**. We respond within one business day.

Include enough detail to reproduce the issue — a proof of concept, the affected
version or commit, and the impact you believe it has.

Please do not open a public issue for a suspected vulnerability. There is no bug
bounty programme. We will credit you in the fix unless you ask us not to.

## What we commit to

- Acknowledgement within one business day.
- An assessment, including whether we agree the issue is exploitable, within five
  business days.
- Any suspected exposure of customer content in the hosted service is treated at
  the highest severity by default, and affected customers are notified without
  waiting for the investigation to conclude.

## Scope

This repository is maintained by Lore Hex Corp. Reports about the hosted
TrustedRouter service, including the attested serving gateways, are in scope at
the same address.

Published enclave measurements and the procedure for verifying what code handles
a request are at <https://trustedrouter.com/trust>. If you believe a published
measurement does not match what is actually running, that is a security report
and we want to hear it.

## Supported versions

The default branch is the supported version. Fixes land there first, and released
packages are built from it.
