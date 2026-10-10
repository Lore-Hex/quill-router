# Model Aliases

`nyte/` is an alternate spelling of `trustedrouter/`. These four model IDs are
equivalent:

- `trustedrouter/auto`
- `trustedrouter/auto-routing`
- `nyte/auto`
- `nyte/auto-routing`

The same namespace alias applies to named models and primitives, for example
`nyte/socrates-2.0` and `trustedrouter/socrates-2.0`. It also applies to custom
`user-*` model IDs. It does not rename models, change versions, or create a new
routing policy. Privacy requirements, routing suffixes, and provider restrictions
are unchanged.

The catalog keeps one canonical entry and publishes accepted spellings in
`trustedrouter.aliases`. Model page links redirect to the canonical page.
Unknown names remain errors, never an implicit request for automatic routing.
