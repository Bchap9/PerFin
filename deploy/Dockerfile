# Multi-stage build: frontend assets -> slim Python runtime. DRAFT.
FROM node:22-alpine AS frontend
WORKDIR /src
COPY frontend/package*.json ./
RUN npm ci --ignore-scripts
COPY frontend/ .
RUN npm run build

FROM python:3.12-slim AS backend
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1
# System deps for extraction: MuPDF, Tesseract, image codecs (worker role).
RUN apt-get update && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-eng libmupdf-dev ghostscript \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/ .
RUN uv sync --frozen --no-dev
COPY --from=frontend /src/dist /app/static

# Non-root runtime user; data dirs owned by it.
RUN groupadd -g 10001 perfin && useradd -u 10001 -g perfin -M -s /usr/sbin/nologin perfin \
    && mkdir -p /var/lib/perfin && chown -R perfin:perfin /var/lib/perfin
USER 10001:10001
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
ENTRYPOINT ["perfin"]
CMD ["api"]
