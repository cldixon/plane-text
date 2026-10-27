# The plane_text pipelines, for running on Cloudflare Containers.
# Build context: the repository root (see buildContext in cloudflare.config.ts).
FROM mirror.gcr.io/library/ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

# The optional build secret `ca` trusts a TLS-intercepting proxy during the build
# only (apt then uses HTTPS, as such proxies refuse plain HTTP); it is never
# written into the image.

# PDF text (pdftotext, pdftoppm) and OCR for the SAIB, AC and maintenance alert
# pipelines; mdbtools (mdb-export) for the NTSB Access databases.
# (mode 0444: apt downloads as the unprivileged _apt user.)
RUN --mount=type=secret,id=ca,required=false,mode=0444 \
    opt=""; \
    if [ -f /run/secrets/ca ]; then \
        sed -i 's|http://|https://|g' /etc/apt/sources.list.d/ubuntu.sources; \
        opt="-o Acquire::https::CAInfo=/run/secrets/ca"; \
    fi; \
    apt-get $opt update \
    && apt-get $opt install -y --no-install-recommends ca-certificates curl poppler-utils tesseract-ocr mdbtools \
    && rm -rf /var/lib/apt/lists/*

# uv, which also installs the Python the lockfile asks for (3.13).
RUN --mount=type=secret,id=ca,required=false \
    cacert=""; if [ -f /run/secrets/ca ]; then cacert="--cacert /run/secrets/ca"; fi; \
    curl $cacert -LsSf https://github.com/astral-sh/uv/releases/download/0.11.32/uv-x86_64-unknown-linux-gnu.tar.gz \
    | tar -xz --strip-components=1 -C /usr/local/bin

COPY pyproject.toml uv.lock .python-version ./
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
