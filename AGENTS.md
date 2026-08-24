# AGENTS.md — start here

Entry point for coding agents (codex-cli, Claude Code) working in `quill-router`.

## Read first

**[`docs/storage-portability/HANDOFF.md`](docs/storage-portability/HANDOFF.md)** — current
state of the multi-cloud + ClickHouse analytics program, what is done, what is next, and the
traps that have already cost real debugging time. If you are picking up that work, read it
before touching anything.

Decision records it depends on:

| Doc | What it settles |
|---|---|
| [`docs/storage-portability/multi-cloud-separation.md`](docs/storage-portability/multi-cloud-separation.md) | Each cloud is a standalone deployment. Identity federates; credits do not. |
| [`docs/storage-portability/analytics-ingestion.md`](docs/storage-portability/analytics-ingestion.md) | How analytics reaches ClickHouse, and the staged plan. |
| [`docs/storage-portability/README.md`](docs/storage-portability/README.md) | Superseded shared-Spanner plan, kept for its tradeoff analysis. |

## Hard rules

**CI has three gates. Run all three locally before claiming done.**

```bash
uv run ruff check . && uv run mypy && uv run pytest -q
```

* **Never run `ruff format .`** — CI only runs `ruff check`, and formatting the tree
  produces an enormous unrelated diff.
* Coverage must stay **≥ 70%**.
* `uv run mypy` is a real gate. Green ruff + pytest locally still fails CI without it.

**Run the FULL suite, not just your new file.** Test pollution here is real: this repo has a
module-global `STORE` proxy that forwards via `__getattr__`, and `monkeypatch.setattr(STORE, ...)`
silently poisons later tests (it once broke 136 of them). Patch the backend **class**, not the
proxy. See issue #333.

**Rebase before you trust a failure.** `main` moves fast (an hourly price-refresh bot commits
to it). A test failing on your branch and passing on `main` usually means your branch is
stale, not that you broke it.

## Deploys

Pushing to `main` **auto-deploys the control plane to Cloud Run**. It triggers only on:
`src/**`, `scripts/deploy/**`, `frontend/src/**`, `Dockerfile`, `.gcloudignore`,
`pyproject.toml`, `uv.lock`, `.github/workflows/deploy.yml`. Docs-only changes do not deploy.

Runtime config is **config-as-code**: the `ENV_VARS` array in
[`scripts/deploy/rollout.sh`](scripts/deploy/rollout.sh). Setting a Cloud Run env var by hand
is a no-op — the next rollout overwrites it.

**Never** run large unbatched DML against production Spanner during a rolling deploy.

## Money code

`reserve` / `settle` / `refund` and anything touching credits are the highest-risk code in the
repo. Changes there need a differential or conformance test, never review-by-eye. Analytics is
explicitly *not* worth destabilising money code for — that is why the analytics outbox enqueue
sits outside the settle transaction and is best-effort.

## Storage backends

`Store` is a Protocol with ~97 methods. Backends must pass
`tests/conformance/` — that suite is the executable contract, and it currently runs `memory`,
`postgres`, and `spanner-pg`. **Do not weaken a conformance test to make a backend pass.**

```bash
docker run -d --rm --name tr-conformance-pg -e POSTGRES_PASSWORD=tr -p 5433:5432 postgres:17
export TR_CONFORMANCE_POSTGRES_DSN=postgresql://postgres:tr@localhost:5433/postgres
```

```bash
docker run -d --rm --name tr-spanner-pg -p 5434:5432 gcr.io/cloud-spanner-pg-adapter/pgadapter-emulator:latest
export TR_CONFORMANCE_SPANNER_PG_DSN=postgresql://localhost:5434/tr-conformance
```

Both need **no cloud credentials**.
