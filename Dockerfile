FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# AWS CLI v2 — only the entrypoint uses it (KMS Decrypt of the cross-cloud
# GCP SA key on AWS ECS deploys; no-op on GCP). Adds ~150 MB to the image
# but keeps the trust story simple: we never store unwrapped SA-key JSON
# at rest, only decrypt it into a tmpfs-backed file at container boot.
# bookworm-slim has libcrypt + glibc; we just need curl + unzip to install.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl unzip ca-certificates \
 && curl -sSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscliv2.zip \
 && unzip -q /tmp/awscliv2.zip -d /tmp \
 && /tmp/aws/install --bin-dir /usr/local/bin --install-dir /usr/local/aws-cli \
 && rm -rf /tmp/aws /tmp/awscliv2.zip \
 && apt-get purge -y --auto-remove curl unzip \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
COPY src ./src
COPY scripts/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

RUN uv sync --frozen --no-dev

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/app/.venv/bin/uvicorn", "trusted_router.main:app", "--host", "0.0.0.0", "--port", "8080"]
