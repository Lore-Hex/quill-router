FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts
COPY frontend.js pages.js setup.mjs session.mjs ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.10.11 AS uv
FROM python:3.12-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY lightning_router ./lightning_router
COPY web ./web
COPY --from=frontend /build/web/app.js ./web/app.js
COPY --from=frontend /build/web/pages.js ./web/pages.js
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin funding
USER 10001:10001
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LR_PAYMENTS_ENABLED=false
EXPOSE 8080
CMD ["uvicorn", "lightning_router.app:from_environment", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--no-proxy-headers"]
