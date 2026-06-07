# Change And Deploy Record

Review date: 2026-06-07

Status: draft.

## Change Process Evidence

CI workflow includes:

- `uv sync --frozen`
- `uv run ruff check .`
- `uv run mypy`
- TypeScript build and dashboard JS diff check
- ESLint
- Stylelint
- `uv run pytest -q`
- Coverage with fail-under 70
- Playwright Chromium install and browser smoke

Deploy workflow includes:

- Workload Identity Federation to GCP.
- Cloud Build image build.
- Cloud Run deployment.
- Serialized deploy concurrency.
- Post-deploy smoke workflow trigger.

## Recent Deploy Evidence

Recent GitHub Actions observations:

- Deploy TR control plane succeeded at 2026-06-07T04:25:44Z for SHA `2cf4578b232121d9e7a27f7b74bd6aad9ee04e96`.
- Deploy TR control plane succeeded at 2026-06-06T23:36:41Z.
- Deploy TR control plane succeeded at 2026-06-06T21:59:26Z.
- Refresh upstream prices runs succeeded on 2026-06-07 and 2026-06-06.

## Exceptions

| Exception | Severity | Required Action |
|---|---|---|
| `main` branch protection is disabled. | High | Enable branch protection before audit date. |
| Prod Smoke has recent failures. | Medium | Open incident/operational event records for each failure cluster and document root cause/remediation. |
| Current compliance packet is not deployed to production. | Medium | Deploy and verify `/legal`, `/legal/procurement.json`, SOC 2 readiness, HIPAA readiness, and subprocessors pages. |

## Emergency Change Record Placeholder

Emergency change:

Reason:

Approver:

Test evidence:

Deploy evidence:

Post-change review:

