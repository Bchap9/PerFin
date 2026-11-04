# scripts

Operational helpers: `dev.sh` (local bootstrap), `backup.sh` / `restore.sh`
(encrypted archives with restore verification). Arrive in Phase 0 and Phase 7.

`synth/statements.py --out DIR [--seed N] [--days N]` writes synthetic
(invented) statements in every supported import format (CSV/TSV layouts,
OFX, QFX, QIF) plus a `manifest.json`; the generator is
`backend/tests/fixtures/synth_statements.py`, shared with the import tests.

`train_classifier.py [--check]` (run from `backend/` with `uv run python
../scripts/train_classifier.py`) retrains the document-kind classifier on
the synthetic corpus of `backend/tests/fixtures/synth_classify.py` and writes
`backend/app/ingestion/classify/artefacts/kind_nb_v1.json`; training is
deterministic and `--check` fails if the shipped artefact is stale.
