# SPDX-License-Identifier: AGPL-3.0-or-later
"""CSV formula-injection neutralisation (SECURITY.md C7).

A spreadsheet treats a cell starting with ``=``, ``+``, ``-`` or ``@`` (and,
in some products, TAB or CR) as a formula. Text that came from a file or a
user is therefore prefixed with a single quote when it starts with one of
those characters, on import (the extractors) and again on export, so a value
read from a statement can never become a formula in someone's spreadsheet.

Plain numbers (``-12.34``, ``+1,000.00``, ``-$5``) are left alone: a negative
amount is data, not a formula, and the spreadsheet reads it as a number.
"""

from __future__ import annotations

import re
from typing import Final

DANGEROUS_PREFIXES: Final = ("=", "+", "-", "@", "\t", "\r")
_NUMBER: Final = re.compile(r"[+-]?[ ]?[$€£¥]?[ ]?\d[\d,.' ]*")
_SIGNS_ONLY: Final = re.compile(r"[+-]+")


def is_dangerous(value: str) -> bool:
    """True when ``value`` would be read as a formula by a spreadsheet."""
    return (
        bool(value)
        and value.startswith(DANGEROUS_PREFIXES)
        and _NUMBER.fullmatch(value) is None
        and _SIGNS_ONLY.fullmatch(value) is None
    )


def neutralise_formula(value: str) -> str:
    """``value`` with a leading ``'`` when it would otherwise be a formula."""
    return "'" + value if is_dangerous(value) else value
