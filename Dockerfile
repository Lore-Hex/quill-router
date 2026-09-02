FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev
# Precompile bytecode into the image. PYTHONDONTWRITEBYTECODE=1 above means a
# running container never writes .pyc, so without this step EVERY cold start
# re-parses and compiles every module in src/ and site-packages on a throttled
# vCPU before uvicorn can bind. Measured on Cloud Run 2026-09-01: startup
# latency p50 34.6s / p95 217s, with 22.7s elapsing between "Starting new
# instance" and uvicorn's first log line while the app's own startup handlers
# took 14ms. compileall writes .pyc explicitly (py_compile), unaffected by the
# env var. src/ must compile cleanly; site-packages is best-effort because a
# few third-party packages ship intentionally-broken example files.
RUN /app/.venv/bin/python -m compileall -q -j 0 /app/src \
 && (/app/.venv/bin/python -m compileall -q -j 0 /app/.venv/lib/python3.12/site-packages >/dev/null 2>&1 || true)

COPY scripts/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/app/.venv/bin/uvicorn", "trusted_router.main:app", "--host", "0.0.0.0", "--port", "8080"]
