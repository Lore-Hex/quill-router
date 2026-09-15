# LightningRouter

Pay with BTC over Lightning. Get USD credits and a TrustedRouter API key.
No email account is required. Existing keys can be funded too.

[Website](https://lightningrouter.ai) |
[Docs](https://lightningrouter.ai/docs) |
[Support and issues](https://github.com/Lore-Hex/lightning-router/issues) |
[Discord](https://discord.gg/FREVts9KAG) |
[@lightningrouter](https://x.com/lightningrouter) |
[@trustedrouter](https://x.com/trustedrouter)

## Source

This repository contains the complete LightningRouter funding application,
frontend, tests, Bitcoin/LND node tooling and deployment runbooks. Lore Hex Corp
operates LightningRouter and TrustedRouter.

* [Application, local setup and tests](experiments/lightning_router/README.md)
* [Funding and deployment](docs/lightning-funding-production.md)
* [Bitcoin node](docs/bitcoin-node.md)
* [Lightning receiving node](docs/lightning-receiving-node.md)
* [Operational scripts](scripts/lightning)

The website is not an inference proxy or a separate billing ledger. It uses
TrustedRouter's USD credit bridge and attested API at
`https://api.trustedrouter.com/v1`. The shared backend remains in
[quill-router](https://github.com/Lore-Hex/quill-router):
[bridge route](https://github.com/Lore-Hex/quill-router/blob/main/src/trusted_router/routes/internal/lightning.py),
[service](https://github.com/Lore-Hex/quill-router/blob/main/src/trusted_router/services/lightning.py),
[storage](https://github.com/Lore-Hex/quill-router/blob/main/src/trusted_router/storage_lightning.py),
and [billing conformance tests](https://github.com/Lore-Hex/quill-router/blob/main/tests/conformance/test_lightning.py).
Use the commit in [SOURCE.json](SOURCE.json) to inspect the matching backend version.

## Releases

Development and production deployment remain in the reviewed quill-router
release pipeline. This repository automatically mirrors the latest successful
LightningRouter CI run on main. It is not a second deployment pipeline.
SOURCE.json records the upstream commit and a SHA-256 hash for every published
file. A passing CI commit is not by itself proof of production deployment.
Only committed, allowlisted source is exported. Credentials, databases,
wallets, node data and backups are never part of the export.

## Support

Use [GitHub Issues](https://github.com/Lore-Hex/lightning-router/issues) for
questions, bug reports and feature requests. Pull requests are disabled and
code contributions are not accepted. See [CONTRIBUTING.md](CONTRIBUTING.md).

Issues and Discord are public. Never post an API key, invoice, payment details
or private prompt. For account or payment help, email support@trustedrouter.com.
Report vulnerabilities privately to security@trustedrouter.com.

## License

Source-available under [Business Source License 1.1](LICENSE), with no additional
production-use grant. Non-production use is permitted under the license;
production use requires separate permission from Lore Hex Corp until the
applicable Change Date. Each version changes to Apache License 2.0 no later
than four years after its first public distribution under BSL. Republishing a
version here does not restart that clock. This is not an OSI-approved
open-source license. Third-party dependencies retain their own licenses; see
[THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES).

Using the hosted LightningRouter service is governed by its
[Terms of Service](https://lightningrouter.ai/terms), not a requirement to buy
a source-code license.
