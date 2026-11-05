## Summary

<!-- What changes and why. Link the roadmap item or issue (docs/ROADMAP.md). -->

## Type of change

- [ ] `feat` - new feature
- [ ] `fix` - bug fix
- [ ] `docs` - documentation only
- [ ] `refactor` / `perf` / `test` / `chore`
- [ ] Breaking change (API, data model, config or upgrade path)

## How it was tested

<!-- Commands run and their result. Name new or changed tests. -->

## Checklist

- [ ] `make check` passes locally (SPDX, ruff, mypy --strict, bandit, pytest, eslint, tsc, vitest, build, audits). Nothing merges red.
- [ ] Commits follow conventional commits (`feat(scope): ...`, `fix(scope): ...`, `docs: ...`)
- [ ] `CHANGELOG.md` updated under the unreleased section (Keep a Changelog)
- [ ] New source files carry the SPDX header (`AGPL-3.0-or-later`)
- [ ] API changed: `make openapi` run and `frontend/src/lib/api/schema.d.ts` regenerated
- [ ] Data model changed: Alembic migration added, `docs/DATA_MODEL.md` updated, upgrade notes in `docs/UPGRADING.md` if needed
- [ ] Helm chart or deploy files changed: `make chart-check` passes
- [ ] Docs updated (`mkdocs build --strict` passes)
- [ ] N/A items above are left unchecked or struck through

## Security and privacy

- [ ] No real financial data, statements, account numbers or credentials in code, fixtures or screenshots (synthetic fixtures only)
- [ ] No new outbound network call, or it is documented in `docs/security/` and gated by the egress profile
- [ ] Auth, permissions, audit logging or secret handling changed: reviewed against `docs/security/CONTROLS.md`
- [ ] New or updated dependencies are pinned and pass `pip-audit` / `npm audit`

## Notes for reviewers

<!-- Anything risky, follow-ups, or areas you want a close look at. -->
