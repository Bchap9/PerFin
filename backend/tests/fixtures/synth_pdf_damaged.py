# SPDX-License-Identifier: AGPL-3.0-or-later
"""A statement PDF with one cross-reference entry that leads nowhere (synthetic).

PDFs that were saved more than once (a filled-in form, a redacted statement)
often keep a place in their cross-reference table for an object that is not
in the file. Nothing on the pages uses that object, and viewers open such
files without a word. :func:`damaged_xref_statement_pdf` builds one: a
one-page reportlab statement, then, byte by byte, an incremental update that
writes the catalog again under a new number and whose cross-reference section
skips the number before it. The table is sized to hold that number, no
section says where its object is, and MuPDF answers "cannot find object in
xref" when asked for it.

With ``javascript`` the update also carries a JavaScript action that the new
catalog runs when the document opens. Both are numbered *after* the entry
that cannot be loaded, so a pass over the objects that stops at the first one
it cannot read never reaches the script.

(An entry that is written out with a wrong offset does not show the damage:
MuPDF rebuilds the table on the first failed read and from then on calls the
object null.)

Every name and figure is invented.
"""

from __future__ import annotations

import importlib
import re
from typing import Any, Final

from tests.fixtures.synth_documents import statement_pdf

#: The heading printed on the page (the statement's first line starts with it).
TITLE: Final = "Harbour Lane Credit Union"
ROWS: Final[tuple[tuple[str, str, str], ...]] = (
    ("2026-03-02", "QUAYSIDE BAKERY", "-6.40"),
    ("2026-03-06", "SYNTHETIC SALARY", "1840.00"),
    ("2026-03-11", "NORTH PIER FERRY", "-23.75"),
    ("2026-03-19", "LANTERN BOOKS", "-17.20"),
)
#: What the script would show; no extraction result may ever carry it.
SCRIPT_MARKER: Final = "synthetic-open-action"


def _one(pattern: bytes, data: bytes) -> bytes:
    found = re.search(pattern, data)
    if found is None:  # pragma: no cover - reportlab changed its output
        raise AssertionError(f"the statement PDF has no {pattern!r}")
    return found.group(1)


def damaged_xref_statement_pdf(javascript: bool) -> bytes:
    """The statement with a cross-reference entry that cannot be loaded (module doc).

    The builder checks its own work with PyMuPDF: the file opens with its one
    page, ``xref_object`` raises for the entry, and (with ``javascript``) the
    catalog names the script. A PyMuPDF that starts to read the entry fails
    here, not in a test that has quietly stopped exercising the damage.
    """
    base = statement_pdf(ROWS, title=TITLE)
    trailer = base[base.rindex(b"trailer") :]
    size = int(_one(rb"/Size (\d+)", trailer))
    info = _one(rb"/Info (\d+ \d+ R)", trailer)
    previous = int(_one(rb"startxref\s+(\d+)", trailer))
    pages = _one(rb"/Pages (\d+ \d+ R)", base)

    out = bytearray(base)
    missing = size  # the first number the statement does not use: never written
    offsets: dict[int, int] = {}
    script = 0
    open_action = b""
    if javascript:
        script = missing + 1
        offsets[script] = len(out)
        out += b"%d 0 obj\n<< /S /JavaScript /JS (app.alert\\('%s'\\)) >>\nendobj\n" % (
            script,
            SCRIPT_MARKER.encode(),
        )
        open_action = b" /OpenAction %d 0 R" % script
    catalog = missing + len(offsets) + 1
    offsets[catalog] = len(out)
    out += b"%d 0 obj\n<< /Type /Catalog /Pages %s%s >>\nendobj\n" % (catalog, pages, open_action)
    xref_at = len(out)
    out += b"xref\n%d %d\n" % (missing + 1, len(offsets))
    for number in sorted(offsets):
        out += b"%010d 00000 n \n" % offsets[number]
    out += b"trailer\n<< /Size %d /Root %d 0 R /Info %s /Prev %d >>\n" % (
        catalog + 1,
        catalog,
        info,
        previous,
    )
    out += b"startxref\n%d\n%%%%EOF\n" % xref_at
    data = bytes(out)
    _check(data, missing=missing, script=script)
    return data


def _check(data: bytes, *, missing: int, script: int) -> None:
    pymupdf: Any = importlib.import_module("pymupdf")  # untyped bindings
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        assert doc.page_count == 1, "the damaged statement lost its page"
        assert missing < doc.xref_length(), "the table has no place for the missing object"
        try:
            doc.xref_object(missing)
        except (RuntimeError, pymupdf.mupdf.FzErrorBase):
            pass
        else:
            raise AssertionError("PyMuPDF loads the entry: the fixture no longer shows the damage")
        if script:
            assert missing < script, "the script must come after the entry that cannot be loaded"
            opened = doc.xref_get_key(doc.pdf_catalog(), "OpenAction")
            assert opened == ("xref", f"{script} 0 R"), opened
            assert doc.xref_get_key(script, "S") == ("name", "/JavaScript")
    finally:
        doc.close()
