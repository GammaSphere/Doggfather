# syntax=docker/dockerfile:1

# ---- build: resolve wheels once so the runtime image never touches PyPI ----
FROM python:3.12-slim AS build
WORKDIR /build
COPY requirements.txt requirements-dev.txt ./
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt \
 && pip wheel --no-cache-dir --wheel-dir /wheels-dev pytest==9.1.1 httpx2==2.13.1

# ---- runtime ---------------------------------------------------------------
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    DOGFOOD_DATA_DIR=/data \
    DOGFOOD_FIXTURES_PATH=/app/fixtures.json
RUN useradd --create-home --uid 10001 dogfood \
 && mkdir -p /data \
 && chown dogfood:dogfood /data
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index /wheels/*.whl && rm -rf /wheels
WORKDIR /app
COPY fixtures.json ./fixtures.json
COPY src ./src
USER dogfood
EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=6 \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)"
CMD ["python", "-m", "doggfather", "serve"]

# ---- test: runtime plus pytest; `docker compose --profile test run --rm tests`
FROM runtime AS test
USER root
COPY --from=build /wheels-dev /wheels-dev
RUN pip install --no-cache-dir --no-index --find-links /wheels-dev pytest httpx2 && rm -rf /wheels-dev
COPY pyproject.toml run.py .dogfood.toml ./
COPY docs ./docs
COPY tests ./tests
USER dogfood
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
