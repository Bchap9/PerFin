# SPDX-License-Identifier: AGPL-3.0-or-later
# Repository gates. `make check` runs every gate; nothing merges red.

SHELL := bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := check

BACKEND := backend
FRONTEND := frontend
PYTHON ?= python3

.PHONY: check spdx backend-check frontend-check audit test backend-test frontend-test \
	dev openapi

## check: every gate (SPDX, backend, frontend, dependency audits)
check: spdx backend-check frontend-check audit

## spdx: licence header on every source file
spdx:
	$(PYTHON) scripts/check_spdx.py

## backend-check: ruff, format, mypy --strict, bandit, pytest, OpenAPI drift
## (also covers scripts/ and deploy/egress/, as the CI backend job does)
backend-check:
	cd $(BACKEND) && uv run ruff check . \
		&& uv run ruff check --config pyproject.toml ../scripts ../deploy/egress
	cd $(BACKEND) && uv run ruff format --check . \
		&& uv run ruff format --check --config pyproject.toml ../scripts ../deploy/egress
	cd $(BACKEND) && uv run mypy \
		&& uv run mypy --strict ../scripts/check_spdx.py ../deploy/egress/egress_proxy.py
	cd $(BACKEND) && uv run bandit -q -c pyproject.toml -r app ../scripts ../deploy/egress
	cd $(BACKEND) && uv run pytest -n auto
	cd $(BACKEND) && uv run perfin openapi | diff -u openapi.json - \
		|| { echo "backend/openapi.json is stale; run 'make openapi'" >&2; exit 1; }

## frontend-check: eslint, tsc, vitest, production build
frontend-check:
	cd $(FRONTEND) && npm run lint && npm run typecheck && npm test && npm run build

## audit: known-vulnerability scans of locked dependencies
audit:
	cd $(BACKEND) && uv run pip-audit
	cd $(FRONTEND) && npm audit --audit-level=high

## test: backend and frontend test suites only
test: backend-test frontend-test

backend-test:
	cd $(BACKEND) && uv run pytest -n auto

frontend-test:
	cd $(FRONTEND) && npm test

## dev: local development stack (scripts/dev.sh)
dev:
	./scripts/dev.sh

## openapi: regenerate backend/openapi.json and the frontend schema types
openapi:
	cd $(BACKEND) && uv run perfin openapi --output openapi.json
	cd $(FRONTEND) && npm run gen:api
