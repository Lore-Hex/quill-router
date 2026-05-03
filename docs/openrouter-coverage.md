# OpenRouter Coverage

`trusted_router.openrouter_coverage.ROUTE_COVERAGE` is the source of truth for
alpha route behavior. CI runs:

```bash
uv run python scripts/check_openrouter_coverage.py
```

That fetches `https://openrouter.ai/openapi.json` and fails if OpenRouter adds
or removes a path/method pair without an explicit TrustedRouter classification.

Classifications:

- `real`: implemented behavior.
- `compatible-real`: intentional compatible behavior that differs by policy,
  such as `/generation/content` returning `content_not_stored`.
- `stub`: explicit alpha unsupported response.
- `deprecated-stub`: explicit deprecated response.

