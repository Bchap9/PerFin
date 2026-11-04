# SPDX-License-Identifier: AGPL-3.0-or-later
"""The review queue (INGESTION.md "Review queue"; API.md ``/review``).

``review_item`` rows live in ``app.domain.documents.models`` (the pipeline
and the importer raise them); this context lists them for the caller and
applies typed resolutions (``service``). ``models`` holds what resolutions
teach the importer (``import_hash_alias``).
"""
