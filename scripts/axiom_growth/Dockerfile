FROM python:3.12-slim
RUN pip install --no-cache-dir google-auth[requests]==2.40.3 httpx==0.28.1
WORKDIR /app
COPY __init__.py model.py runtime.py /app/scripts/axiom_growth/
USER 65534:65534
CMD ["python", "-m", "scripts.axiom_growth.runtime"]
