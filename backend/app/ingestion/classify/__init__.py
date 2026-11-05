# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ingestion stage: classify (kind, institution, account, period; INGESTION.md "Classify").

Fingerprints (:mod:`.fingerprints`, shipped YAML in ``fingerprints/`` and
the household's ``parser_fingerprint`` rows), then the heuristic naive Bayes
classifier (:mod:`.model`, artefact in ``artefacts/``), then review. Entry
points: :func:`.service.classify_extraction` (pure) and
:func:`.service.classify_document` (writes the ``document`` row).
"""
