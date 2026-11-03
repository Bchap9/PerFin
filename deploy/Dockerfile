# SPDX-License-Identifier: AGPL-3.0-or-later
#
# PerFin application image: one image, three roles (api, worker, mcp) plus the
# one-shot `migrate` command. Build from the repository root:
#
#   docker build -f deploy/Dockerfile -t perfin:dev .
#
# Base images are pinned by tag; the digest each tag resolved to when this
# file was last reviewed is noted next to it. The weekly security workflow
# rebuilds with --pull and fails on fixable HIGH/CRITICAL findings. Every base
# is an ARG so a mirror can be used without editing this file, e.g. when
# Docker Hub rate-limits anonymous pulls:
#
#   docker build -f deploy/Dockerfile \
#     --build-arg NODE_IMAGE=mirror.gcr.io/library/node:22-alpine \
#     --build-arg PYTHON_IMAGE=mirror.gcr.io/library/python:3.12-slim .
#
# Security properties (SECURITY.md H1, H4, H5; ADR-0006):
# - runs as UID/GID 10001, never root; works with a read-only root filesystem
#   (no bytecode or cache writes at runtime, scratch space is /tmp tmpfs);
# - no build tools, compilers, package caches, uv or Node in the runtime stage;
# - no secrets: configuration arrives at runtime through the environment or
#   mounted files (.dockerignore keeps env files and keys out of the context).

# node:22-alpine     sha256:fe4f6d74e20370acb04ad889077d14d0f64a9c8ea6a42bc60cddf1e844699914
ARG NODE_IMAGE=docker.io/library/node:22-alpine
# python:3.12-slim   sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30
ARG PYTHON_IMAGE=docker.io/library/python:3.12-slim
# uv, pinned to the version that wrote backend/uv.lock (never :latest)
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.32

# ---------------------------------------------------------------------------
FROM ${UV_IMAGE} AS uv

# ---------------------------------------------------------------------------
# Frontend: compile the SPA with the exact locked dependency tree.
FROM ${NODE_IMAGE} AS frontend
WORKDIR /src
ENV CI=true \
    NPM_CONFIG_AUDIT=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_UPDATE_NOTIFIER=false
COPY frontend/package.json frontend/package-lock.json frontend/.npmrc ./
# `npm ci` fails if package-lock.json and package.json disagree (F10);
# lifecycle scripts of dependencies are not run.
RUN npm ci --ignore-scripts
COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Backend build: resolve the hash-locked environment into /app/.venv.
FROM ${PYTHON_IMAGE} AS backend
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Dependencies first (cached layer), then the project itself. --frozen
# installs exactly uv.lock (hashes verified) and fails rather than re-resolve.
COPY backend/pyproject.toml backend/uv.lock backend/README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/alembic.ini ./
COPY backend/app ./app
COPY backend/migrations ./migrations
RUN uv sync --frozen --no-dev \
    && /app/.venv/bin/python -m compileall -q -j 0 /app/app /app/migrations

# ---------------------------------------------------------------------------
# Runtime.
FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="PerFin" \
      org.opencontainers.image.description="Self-hosted, security-first personal finance" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.source="https://github.com/Bchap9/PerFin"

# Only what the worker's extraction sandbox needs (INGESTION.md, ADR-0006):
# Tesseract OCR with English data, and libseccomp for the subprocess filter.
# PyMuPDF and pillow-heif wheels bundle their native libraries. Pending
# security updates for the base are applied at build time; weekly rebuilds
# (security.yml) pick up new ones.
RUN apt-get update --error-on=any \
    && apt-get -y upgrade \
    && apt-get install -y --no-install-recommends \
         tesseract-ocr \
         tesseract-ocr-eng \
         libseccomp2 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* /var/log/apt /var/log/dpkg.log

# Unprivileged runtime identity. No login shell, no home directory contents.
# The data directories are created here, owned by 10001, so that named
# volumes mounted over them start out writable by the app.
RUN groupadd --system --gid 10001 perfin \
    && useradd --system --uid 10001 --gid 10001 --no-create-home \
         --home-dir /nonexistent --shell /usr/sbin/nologin perfin \
    && install -d -o 10001 -g 10001 -m 0750 \
         /var/lib/perfin \
         /var/lib/perfin/documents \
         /var/lib/perfin/quarantine \
         /var/lib/perfin/processed \
    && install -d -o 10001 -g 10001 -m 0550 /var/lib/perfin/inbox

# Application code and environment are owned by root and read-only for the
# runtime user: a compromised process cannot modify its own code.
COPY --from=backend --chown=root:root /app /app
COPY --from=frontend --chown=root:root /src/dist /app/static

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PERFIN_BIND_HOST=0.0.0.0 \
    PERFIN_BIND_PORT=8000 \
    PERFIN_STATIC_DIR=/app/static \
    PERFIN_DROPFOLDER_INBOX=/var/lib/perfin/inbox \
    PERFIN_DROPFOLDER_PROCESSED=/var/lib/perfin/processed \
    TMPDIR=/tmp

WORKDIR /app
USER 10001:10001

# API port. Metrics (PERFIN_METRICS_PORT, 9100) bind to the container's
# loopback by default (PERFIN_METRICS_BIND_HOST) and are deliberately not
# declared here.
EXPOSE 8000

# Probes 127.0.0.1:/healthz of the API role. Compose replaces this check for
# the worker and disables it for one-shot commands.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["perfin", "healthcheck"]

STOPSIGNAL SIGTERM
ENTRYPOINT ["perfin"]
CMD ["api"]
