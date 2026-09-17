FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts
COPY frontend.js pages.js setup.mjs session.mjs chrome.mjs ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.10.11 AS uv
FROM debian:bookworm-slim AS lexe
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl unzip && rm -rf /var/lib/apt/lists/*
RUN curl --fail --location --proto '=https' --tlsv1.2 \
      https://github.com/lexe-app/lexe-sidecar-sdk/releases/download/lexe-sidecar-v0.4.20/lexe-sidecar-linux-x86_64.zip \
      -o /tmp/lexe.zip \
    && echo '3417ce147e1d0bd9db7e328dd42526338a6dbf874762700a010806e7b09e1f49  /tmp/lexe.zip' | sha256sum -c - \
    && unzip /tmp/lexe.zip -d /opt/lexe && chmod 0555 /opt/lexe/lexe-sidecar
FROM python:3.12-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=lexe /opt/lexe/lexe-sidecar /usr/local/bin/lexe-sidecar
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY lightning_router ./lightning_router
COPY LICENSE THIRD_PARTY_NOTICES ./
COPY web ./web
COPY --from=frontend /build/web/app.js ./web/app.js
COPY --from=frontend /build/web/pages.js ./web/pages.js
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin funding
RUN mkdir -p /var/lib/lexe && chown 10001:10001 /var/lib/lexe && chmod 0700 /var/lib/lexe
USER 10001:10001
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LR_PAYMENTS_ENABLED=false
EXPOSE 8080
CMD ["python", "-m", "lightning_router.launch"]
