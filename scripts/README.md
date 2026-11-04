# scripts

Operational helpers: `dev.sh` (local bootstrap), `backup.sh` / `restore.sh`
(encrypted archives with restore verification). Arrive in Phase 0 and Phase 7.

`synth/statements.py --out DIR [--seed N] [--days N]` writes synthetic
(invented) statements in every supported import format (CSV/TSV layouts,
OFX, QFX, QIF) plus a `manifest.json`; the generator is
`backend/tests/fixtures/synth_statements.py`, shared with the import tests.
