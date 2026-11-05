# SPDX-License-Identifier: AGPL-3.0-or-later
#
# PerFin machine-learning adapter (optional; ADR-0010, docs/ML.md). A single
# standard-library Python file on the same pinned base as the application
# image; no packages are installed. The model itself runs in the `ollama`
# runtime container next to it. Build context is this directory:
#
#   docker build -t perfin-ml:dev deploy/ml
#
# python:3.12-slim   sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30
ARG PYTHON_IMAGE=docker.io/library/python:3.12-slim
FROM ${PYTHON_IMAGE}

LABEL org.opencontainers.image.title="PerFin ML adapter" \
      org.opencontainers.image.description="Optional local-model document hints for the PerFin worker" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.source="https://github.com/Bchap9/PerFin"

RUN groupadd --system --gid 10001 perfinml \
    && useradd --system --uid 10001 --gid 10001 --no-create-home \
         --home-dir /nonexistent --shell /usr/sbin/nologin perfinml \
    && install -d -m 0755 /opt/perfin-ml

COPY --chown=root:root --chmod=0644 perfin_ml.py /opt/perfin-ml/perfin_ml.py

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PERFIN_ML_LISTEN=0.0.0.0:3003

USER 10001:10001
EXPOSE 3003
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-I", "/opt/perfin-ml/perfin_ml.py", "healthcheck"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "-I", "/opt/perfin-ml/perfin_ml.py"]
