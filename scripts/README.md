# scripts

Operational helpers: `dev.sh` (the local dev stack, `make dev`; `up|seed|down|reset`,
see docs/DEVELOPMENT.md "Dev stack"), `seed_dev.py` (the dev seed, refuses unless
`PERFIN_DEV_MODE=true`; run from `backend/` as `uv run python ../scripts/seed_dev.py`),
`backup.sh` / `restore.sh`
(passphrase-encrypted backup archives and a verified restore into a fresh
stack, for Docker Compose or a bare-metal install; see
docs/BACKUP_RESTORE.md, which also covers `perfin rotate-key`). The restore
verification test `backend/tests/integration/test_backup_restore.py` runs
both scripts in `--bare` mode.

`synth/statements.py --out DIR [--seed N] [--days N]` writes synthetic
(invented) statements in every supported import format (CSV/TSV layouts,
OFX, QFX, QIF) plus a `manifest.json`; the generator is
`backend/tests/fixtures/synth_statements.py`, shared with the import tests.

`train_classifier.py [--check]` (run from `backend/` with `uv run python
../scripts/train_classifier.py`) retrains the document-kind classifier on
the synthetic corpus of `backend/tests/fixtures/synth_classify.py` and writes
`backend/app/ingestion/classify/artefacts/kind_nb_v1.json`; training is
deterministic and `--check` fails if the shipped artefact is stale.

`install-chart-tools.sh <dir>` builds the pinned helm and kubeconform used by
`make chart-check`, the CI `helm` job, `backend/tests/unit/test_helm_chart.py`
and the release workflow, from Go modules whose hashes are pinned in the
script. `release_notes.py check|notes <tag>` is the release workflow's tag,
version and CHANGELOG check and prints the release notes (docs/UPGRADING.md).
