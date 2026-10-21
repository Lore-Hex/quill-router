FROM python:3.12-slim-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && sed -i -e 's|^mozilla/emSign_ECC_Root_CA_-_G3.crt$|!&|' \
           -e 's|^mozilla/Comodo_AAA_Services_root.crt$|!&|' /etc/ca-certificates.conf \
 && update-ca-certificates --fresh \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir 'certifi>=2026.10.19' google-auth[requests]==2.40.3 httpx==0.28.1
WORKDIR /app
COPY __init__.py model.py gateway_attempts.py runtime.py /app/scripts/axiom_growth/
USER 65534:65534
CMD ["python", "-m", "scripts.axiom_growth.runtime"]
