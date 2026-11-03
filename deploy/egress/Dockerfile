# SPDX-License-Identifier: AGPL-3.0-or-later
#
# PerFin egress allowlist proxy (SECURITY.md H2). A single standard-library
# Python file on the same pinned base as the application image; no packages
# are installed. Build context is this directory:
#
#   docker build -t perfin-egress:dev deploy/egress
#
# python:3.12-slim   sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30
ARG PYTHON_IMAGE=docker.io/library/python:3.12-slim
FROM ${PYTHON_IMAGE}

LABEL org.opencontainers.image.title="PerFin egress proxy" \
      org.opencontainers.image.description="CONNECT-only allowlisting forward proxy for the PerFin worker" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.source="https://github.com/Bchap9/PerFin"

RUN groupadd --system --gid 10001 egress \
    && useradd --system --uid 10001 --gid 10001 --no-create-home \
         --home-dir /nonexistent --shell /usr/sbin/nologin egress \
    && install -d -m 0755 /opt/egress /etc/egress \
    && printf '# empty allowlist: every destination is refused\n' > /etc/egress/allowlist.txt

COPY --chown=root:root --chmod=0644 egress_proxy.py /opt/egress/egress_proxy.py

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EGRESS_LISTEN=0.0.0.0:3128 \
    EGRESS_ALLOWLIST=/etc/egress/allowlist.txt

USER 10001:10001
EXPOSE 3128
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-I", "/opt/egress/egress_proxy.py", "healthcheck"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "-I", "/opt/egress/egress_proxy.py"]
