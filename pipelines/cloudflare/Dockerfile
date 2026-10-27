# The plane_text pipelines, for running on Cloudflare Containers.
# Build context: the repository root (see buildContext in cloudflare.config.ts).
FROM mirror.gcr.io/library/python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

# An optional build secret `ca` trusts a TLS-intercepting proxy during the build
# only; it is never written into the image.
RUN --mount=type=secret,id=ca,required=false \
    if [ -f /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca SSL_CERT_FILE=/run/secrets/ca; fi; \
    pip install --no-cache-dir uv==0.11.32

COPY pyproject.toml uv.lock ./
RUN --mount=type=secret,id=ca,required=false \
    if [ -f /run/secrets/ca ]; then export SSL_CERT_FILE=/run/secrets/ca; fi; \
    uv sync --locked --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN --mount=type=secret,id=ca,required=false \
    if [ -f /run/secrets/ca ]; then export SSL_CERT_FILE=/run/secrets/ca; fi; \
    uv sync --locked --no-dev

# Pipeline outputs and download caches, discarded with the container.
WORKDIR /work

# The Worker passes the pipeline's own command as the entrypoint; this is a smoke test.
CMD ["python", "-c", "import plane_text.faa_ads; print('plane_text ready')"]
