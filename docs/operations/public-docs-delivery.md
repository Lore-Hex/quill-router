# Public documentation delivery

The public `/docs` page is a deployed template, not a repository-only Markdown
document. Previously every edit under `src/` rebuilt and rolled the API first,
then published the separate public service. The November 16 Rampart docs release
illustrated the cost: about 44 minutes of merged-commit CI, admission and release
selection failures, then about 56 minutes of API regional rollout before the
six-minute public publication. The GitHub merge incident was a separate delay.

## Narrow publication lane

`deploy.yml` still requires successful **full CI for the exact commit** and the
normal immutable Cloud Build artifact. Neither CI nor its required checks changes.
Only an automatic main push modifying
`src/trusted_router/templates/public/docs.html`, optionally together with ordinary
files under `tests/` and `docs/`, is a candidate. Docs-page deletion or renaming,
executable or symlink modes, dependencies, other templates, application code, and deployment
configuration do not qualify.

`deploy-public-docs.yml` shares production workflow ordering and acquires an
**exclusive public-surface lease**, retaining the normal healthy-holdback gate.
It does not interrupt a gateway or control-plane owner. Before and after admission,
it reads the actual 100%-traffic, ready, immutable-image revision in **all four API
and four public regions**. Every source must be an ancestor of the candidate and
have an identical repository tree outside the narrow docs/test allowlist. Thus a
docs-only last commit cannot hide earlier, still-undelivered application changes.
A known code difference falls back to the ordinary full rollout; unavailable or
ambiguous source/health evidence fails closed.

The publisher revalidates main ancestry, the original build manifest and exact CI
immediately before publication. It then uses the existing regional public deploy:
no-traffic candidate, bounded checks including `/docs`, promotion, ingress closure,
probe-tag cleanup and durable rollback handling. It changes no API revision,
schema, runtime-secret sync job, worker, URL map or attestation pin.

Final verification requires exact public source/image convergence, unchanged API
revision/configuration snapshots, public route checks, the client-IP/ingress audit,
and the existing cloud-completeness gate. A failure after mutation retains the
reservation and failure evidence. Do not recover a failed owner without the
ordinary recovery checks and authority.

## Two source identities

`TR_RELEASE` on the public service continues to identify the API, since it is used
by the public control-plane trust document and secondary-cloud reconciliation.
`TR_PUBLIC_SOURCE_COMMIT` records the website's own source. The `public-release-N`
artifact is **public delivery only**, never evidence that the API or other clouds
were deployed. Full release selection treats it as a website no-downgrade floor;
the full companion also checks the actual serving public source before writing,
including when an earlier receipt upload failed.

## Expected improvement and limits

An eligible docs update no longer waits for or executes schema migrations, secret
sync, API 10/50/100 ramps, secondary API ramps or worker deployment. All website
safety checks remain. CI, a genuine active-owner wait, image build, regional website
publication and health checks still take time. The historical timings are not a
benchmark or a promise for future runs. Mixed changes deliberately retain the full
release path.
