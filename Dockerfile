# Courtroom IndicXlit — container images for any Linux VM.
#
# Targets (one Dockerfile, three images):
#   offline  Tier A typist service + editor UI, model baked in. Boots without central.  (default)
#   central  Central Model Registry with the published package baked in.
#   local    Tier A typist service with an empty store; downloads from central, then serves.
#
#   make docker-build          # stage model bytes + build all three
#   make docker-run            # offline image on http://localhost:$(DOCKER_LOCAL_PORT)/
#   make docker-stack-up       # central + local via docker compose (download-then-serve)
#
# Model bytes come from docker/preloaded_{registry,store}, staged by
# scripts/prepare_docker_assets.py from an already-published var/registry.

ARG PYTHON_IMAGE=python:3.10-slim-bookworm

FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    XLIT_LOG_LEVEL=INFO \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2

WORKDIR /app

# Behind TLS inspection (e.g. Zscaler) drop host-exported *.crt files into
# docker/certs/ so apt/pip can reach the internet during the build. Empty is fine.
COPY docker/certs/ /usr/local/share/ca-certificates/corp/
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates build-essential git \
    && (cp /usr/local/share/ca-certificates/corp/*.crt /usr/local/share/ca-certificates/ 2>/dev/null || true) \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt

# Same two steps as `make setup`: runtime deps resolved normally, then the ML
# freeze with --no-deps (fairseq/omegaconf metadata needs pip<24.1).
# torch comes from the CPU index: PyPI's linux/amd64 wheel is the CUDA build and
# fails at import without the nvidia-* libraries that --no-deps skips.
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
COPY requirements-runtime.txt requirements-ml.txt /app/
RUN python -m pip install 'pip<24.1' \
    && python -m pip install -r requirements-runtime.txt \
    && python -m pip install --no-deps --index-url "$TORCH_INDEX_URL" "$(grep '^torch==' requirements-ml.txt)" \
    && grep -v '^torch==' requirements-ml.txt > requirements-ml-notorch.txt \
    && python -m pip install --no-deps -r requirements-ml-notorch.txt \
    && rm requirements-ml-notorch.txt \
    && python -c "import ai4bharat.transliteration.transformer.base_engine, ai4bharat.transliteration.transformer.custom_interactive" \
    && apt-get purge -y --auto-remove build-essential git

RUN useradd --system --uid 10001 --home-dir /app xlit \
    && mkdir -p /app/var /app/logs \
    && chown -R xlit:xlit /app/var /app/logs

# Checkout modes vary with the host umask; the service runs as a non-root user.
COPY xlit /app/xlit
RUN chmod -R a+rX /app/xlit


FROM runtime AS central
COPY --chown=xlit:xlit docker/preloaded_registry /app/var/registry
USER xlit
EXPOSE 8700
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=12 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8700/healthz', timeout=2).status == 200 else 1)"
CMD ["python", "-m", "xlit.central.server", \
     "--host", "0.0.0.0", "--port", "8700", \
     "--registry", "/app/var/registry", "--log-dir", "/app/logs"]


# Empty store: bootstraps from $XLIT_CENTRAL_URL, then serves. Short poll while bootstrapping.
FROM runtime AS local
ENV XLIT_CENTRAL_URL=http://central:8700 \
    XLIT_RESCORE=off
# Named volumes inherit this ownership on first mount.
RUN mkdir -p /app/var/local_store && chown xlit:xlit /app/var/local_store
USER xlit
EXPOSE 8765
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=60 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=2).status == 200 else 1)"
CMD ["sh", "-c", "exec python -m xlit.local.server --host 0.0.0.0 --port 8765 \
     --central-url \"$XLIT_CENTRAL_URL\" --rescore \"$XLIT_RESCORE\" \
     --store-dir /app/var/local_store --log-dir /app/logs --poll-interval 15"]


# Preloaded store, central pointed at a closed port: boots offline from disk.
FROM runtime AS offline
COPY --chown=xlit:xlit docker/preloaded_store /app/var/local_store
ENV XLIT_CENTRAL_URL=http://127.0.0.1:9 \
    XLIT_RESCORE=off
USER xlit
EXPOSE 8765
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=12 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=2).status == 200 else 1)"
CMD ["sh", "-c", "exec python -m xlit.local.server --host 0.0.0.0 --port 8765 \
     --central-url \"$XLIT_CENTRAL_URL\" --rescore \"$XLIT_RESCORE\" \
     --store-dir /app/var/local_store --log-dir /app/logs --poll-interval 3600"]
