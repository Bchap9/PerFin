# SPDX-License-Identifier: AGPL-3.0-or-later
"""Operator tooling behind the ``perfin`` CLI: key rotation, backup encryption, restore checks.

Nothing here serves traffic. The commands are registered on the CLI parser by
:func:`app.ops.commands.register` (docs/BACKUP_RESTORE.md).
"""
