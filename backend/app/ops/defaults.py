# SPDX-License-Identifier: AGPL-3.0-or-later
"""Defaults of the operator commands, importable without the database or key modules.

``app.cli.build_parser`` registers the operator commands in every process,
including ``perfin mcp``, which must never load ``app.db`` or the key
modules (SECURITY.md E4); the parser needs only these numbers.
"""

from __future__ import annotations

from typing import Final

#: Random stored documents ``perfin restore-check`` decrypts.
DEFAULT_SAMPLE: Final = 20
#: Data keys ``perfin rotate-key`` rewraps per committed batch.
DEFAULT_BATCH_SIZE: Final = 500
