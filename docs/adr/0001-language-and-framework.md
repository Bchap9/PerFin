# ADR-0001: Python/FastAPI backend, React/TypeScript frontend, modular monolith

Status: Accepted. Date: 2026-11-03.

## Context
PerFin needs (a) a web API with strong input validation, (b) heavy document
processing (PDF parsing, OCR, table extraction, light ML), (c) a dynamic
single-page UI, and (d) a small deployment footprint for self-hosters. It will
be developed by a very small team.

## Decision
- Backend in **Python 3.12** with **FastAPI**, **Pydantic v2** for boundary
  validation, **SQLAlchemy 2** and **Alembic**; dependency management with
  **uv** and a hash-locked lockfile.
- Frontend in **React 18 + TypeScript**, built with **Vite**, using
  **TanStack Query** and **TanStack Router**; API client generated from the
  OpenAPI document.
- A **modular monolith**: one repository, one image, two roles (api, worker)
  selected by entrypoint. Domain packages with explicit boundaries; no
  microservices.

## Alternatives considered
- **Go or Rust backend.** Better memory safety and smaller images, and
  attractive for the security story. Rejected for the primary codebase because
  the document-processing ecosystem (PyMuPDF, ocrmypdf, pdfplumber, scikit
  learn, OFX libraries) is far stronger in Python and that is where most of
  the engineering effort will go. Revisit: a Rust extraction sidecar is a
  plausible later hardening step for the sandbox.
- **TypeScript everywhere (Node backend).** Single language, but the same
  document-processing gap, and Python's typing story is now good enough with
  Pydantic and mypy.
- **Django.** Mature auth and admin, but FastAPI's async model, OpenAPI
  generation and Pydantic integration fit the API-plus-SPA shape better, and
  we are building auth deliberately rather than inheriting it.
- **SvelteKit / HTMX.** Smaller bundles. Rejected in favour of the larger
  React ecosystem for data grids, charts and WebAuthn libraries.
- **Separate services for ingestion.** Premature. The worker role already
  isolates untrusted content handling; splitting the codebase adds operational
  burden for self-hosters.

## Consequences
- Memory-unsafe native libraries (MuPDF, Tesseract, image codecs) run in
  Python; ADR-0006 confines them to a sandboxed subprocess in the worker.
- Python startup and image size are acceptable; multi-stage build keeps the
  runtime image slim.
- Type checking (mypy strict on the backend, `tsc --strict` on the frontend)
  is mandatory in CI to compensate for dynamic typing.
