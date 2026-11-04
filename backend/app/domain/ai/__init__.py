# SPDX-License-Identifier: AGPL-3.0-or-later
"""AI access boundary (AI_ACCESS.md, ADR-0007): financial facts exposed, identity never.

``models``: ``ai_client`` and ``ai_call``. ``classification``: the single
registry classifying every database column as exposed, free text (scrubbed)
or denied. ``policy``: a client's mode and options applied to values.
``service``: the AI-client flow (the only code that mints an ``ai:read``
token), per-client rate limits and the call audit. ``views``: the read-only
views behind ``/api/v1/ai/v1``, built from the allowlist schemas in
``schemas``. PerFin never calls a language model.
"""
