# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generators for the hostile upload corpus (all synthetic, built in memory).

Each :class:`Sample` says what the intake and the sandbox must do with it:

* ``rejected``: refused at intake with this :class:`RejectReason` (the API
  answers ``415``; nothing is stored);
* ``extract_error``: accepted, and the extractor answers ``ok=false`` with one
  of these codes (or the sandbox stops it with one of these sandbox codes);
* ``ok``: accepted and extracted (``safe`` checks hold on the result);
* ``contained``: any outcome, as long as it is a validated result or a
  sandbox resource-limit error within the time budget.

Families: malformed PDFs (truncated xref, reference loops, deep nesting,
stream bombs, JavaScript and launch actions), ZIP bombs (flat high ratio,
42.zip-style nesting, overlapping members, too many members), polyglots
(PDF+HTML, PNG+ZIP, GIFAR-like, JPEG with script, PDF+ZIP), XML attacks
(XXE and billion laughs in OFX and XLSX, plain XML), oversized images
(header-declared and real pixel bombs, PNG/JPEG/TIFF), malformed mail
(broken MIME, deep multipart, bad base64, header floods, traversal names),
and archives or executables disguised under an accepted name (tar, 7z,
gzip, rar, ELF, PE).
"""

from __future__ import annotations

import io
import struct
import tarfile
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.policy import default as default_policy
from typing import Final, Literal

from tests.fixtures import synth_documents as synth
from tests.fixtures.statements import XXE_PROLOG, csv_bytes, ofx_xml, synth_transactions
from tests.fixtures.synth_xlsx import inline_row, raw_xlsx, sheet_xml

Expect = Literal["rejected", "extract_error", "ok", "contained"]

#: Sandbox error codes that mean "stopped by a resource limit" (contained).
LIMIT_CODES: Final = frozenset(
    {"timeout", "cpu_limit", "memory_limit", "killed", "output_too_large", "input_too_large"}
)


@dataclass(frozen=True)
class Sample:
    name: str
    build: Callable[[], bytes]
    expect: Expect
    #: ``rejected``: the reject reasons; ``extract_error``: accepted codes.
    codes: frozenset[str] = field(default_factory=frozenset)
    #: Text that must never appear in a validated result (C6/C8 leaks).
    forbidden: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# PDFs
# ---------------------------------------------------------------------------


def _pdf(objects: list[bytes], *, root: int = 1, xref: bool = True, trailer: bytes = b"") -> bytes:
    """A PDF from numbered object bodies (1-based), with a correct xref by default."""
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    start = len(out)
    if xref:
        out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
        for offset in offsets:
            out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {root} 0 R ".encode()
        + trailer
        + f">>\nstartxref\n{start}\n%%EOF\n".encode()
    )
    return bytes(out)


def _page_objects(content: bytes, *, filter_: bytes = b"") -> list[bytes]:
    return [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 "
        b"/BaseFont /Helvetica >> >> >> >>",
        b"<< /Length "
        + str(len(content)).encode()
        + filter_
        + b" >>\nstream\n"
        + content
        + b"\nendstream",
    ]


TEXT_PAGE = b"BT /F1 12 Tf 72 720 Td (Fuzz corpus page) Tj ET"


def pdf_truncated_xref() -> bytes:
    data = _pdf(_page_objects(TEXT_PAGE))
    return data[: data.index(b"xref") + 20]


def pdf_wrong_startxref() -> bytes:
    data = _pdf(_page_objects(TEXT_PAGE))
    return data.replace(b"startxref\n", b"startxref\n99999999")


def pdf_reference_loop() -> bytes:
    # The page tree is its own kid, and the page its own parent.
    return _pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [2 0 R 3 0 R] /Count 2 /Parent 2 0 R >>",
            b"<< /Type /Page /Parent 3 0 R /Kids [3 0 R] /MediaBox [0 0 612 792] >>",
        ]
    )


def pdf_indirect_length_loop() -> bytes:
    objects = _page_objects(TEXT_PAGE)
    objects[3] = b"<< /Length 5 0 R >>\nstream\n" + TEXT_PAGE + b"\nendstream"
    return _pdf([*objects, b"5 0 R"])


def pdf_deep_nesting(depth: int = 50_000) -> bytes:
    deep = b"[" * depth + b"]" * depth
    return _pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R /Deep " + deep + b" >>",
            b"<< /Type /Pages /Kids [] /Count 0 >>",
        ]
    )


def pdf_stream_bomb(megabytes: int = 400) -> bytes:
    """A page whose content stream inflates to ``megabytes`` of zeros (about 1000:1)."""
    compressor = zlib.compressobj(9)
    chunk = b"\x00" * (1024 * 1024)
    parts = [compressor.compress(chunk) for _ in range(megabytes)]
    parts.append(compressor.flush())
    return _pdf(_page_objects(b"".join(parts), filter_=b" /Filter /FlateDecode"))


def pdf_javascript() -> bytes:
    objects = _page_objects(TEXT_PAGE)
    objects[0] = (
        b"<< /Type /Catalog /Pages 2 0 R /OpenAction 5 0 R "
        b"/Names << /JavaScript << /Names [(x) 5 0 R] >> >> /AA << /WC 6 0 R >> >>"
    )
    objects.append(b"<< /S /JavaScript /JS (app.alert('perfin-fuzz-js-marker')) >>")
    objects.append(b"<< /S /Launch /F (perfin-fuzz-launch-marker.exe) >>")
    return _pdf(objects)


def pdf_huge_object_count() -> bytes:
    objects = _page_objects(TEXT_PAGE) + [b"null"] * 200
    data = _pdf(objects)
    # Claim far more objects than exist.
    return data.replace(f"/Size {len(objects) + 1}".encode(), b"/Size 99999999")


def pdf_huge_mediabox() -> bytes:
    objects = _page_objects(TEXT_PAGE)
    objects[2] = objects[2].replace(b"[0 0 612 792]", b"[0 0 14400 14400] /UserUnit 75000")
    return _pdf(objects)


# ---------------------------------------------------------------------------
# ZIP bombs
# ---------------------------------------------------------------------------


def zip_flat_bomb() -> bytes:
    return synth.zip_bomb(96 * 1024 * 1024)


def zip_nested_42(levels: int = 3, fan: int = 8) -> bytes:
    """42.zip-style: each level holds ``fan`` copies of the level below."""
    data = synth.zip_bomb(8 * 1024 * 1024)
    for level in range(levels):
        data = synth.zip_bytes([(f"l{level}-{i}.zip", data) for i in range(fan)])
    return data


def zip_overlapping() -> bytes:
    """Several central directory entries that all point at one local file (Fifield)."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("a.csv", b"Date,Amount\n" + b"2026-01-01,1.00\n" * 50_000)
    data = out.getvalue()
    cd = data.index(b"PK\x01\x02")
    eocd = data.index(b"PK\x05\x06")
    entry = data[cd:eocd]
    copies = 20
    central = b"".join(entry.replace(b"a.csv", f"{i:05d}".encode()[:5], 1) for i in range(copies))
    end = bytearray(data[eocd:])
    struct.pack_into("<HHII", end, 8, copies, copies, len(central), cd)
    return data[:cd] + central + bytes(end)


def zip_many_members(count: int = 5000) -> bytes:
    return synth.zip_bytes(
        [(f"m{i}.csv", b"") for i in range(count)], compression=zipfile.ZIP_STORED
    )


def zip_traversal_names() -> bytes:
    member = b"Date,Amount\n2026-01-01,1.00\n"
    return synth.zip_bytes(
        [
            ("../../../../etc/cron.d/x.csv", member),
            ("/abs/path.csv", member),
            ("C:\\win.csv", member),
        ]
    )


# ---------------------------------------------------------------------------
# Polyglots
# ---------------------------------------------------------------------------


def pdf_html_polyglot() -> bytes:
    return (
        b"%PDF-1.4\n<html><script>alert(document.domain)</script></html>\n"
        + _pdf(_page_objects(TEXT_PAGE))[9:]
    )


def html_then_pdf() -> bytes:
    return b"<html><body>" + _pdf(_page_objects(TEXT_PAGE)) + b"</body></html>"


def png_zip_polyglot() -> bytes:
    from PIL import Image

    png = synth.image_bytes(Image.new("L", (64, 64), 255), "PNG")
    return png + synth.zip_bytes([("payload.html", b"<script>alert(1)</script>")])


def gifar() -> bytes:
    gif = b"GIF89a\x01\x00\x01\x00\x00\x00\x00;"
    return gif + synth.zip_bytes([("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\n")])


def jpeg_with_script_comment() -> bytes:
    from PIL import Image

    jpeg = synth.image_bytes(Image.new("RGB", (32, 32), (200, 10, 10)), "JPEG")
    comment = b"<script>alert(1)</script>"
    segment = b"\xff\xfe" + struct.pack(">H", len(comment) + 2) + comment
    return jpeg[:2] + segment + jpeg[2:]


def pdf_zip_polyglot() -> bytes:
    return _pdf(_page_objects(TEXT_PAGE)) + synth.zip_bytes([("x.csv", b"a,b\n1,2\n")])


def csv_with_script_past_window() -> bytes:
    return (
        csv_bytes(synth_transactions(400, seed=4)) + b"2026-02-01,1.00,<svg onload=alert(1)>,x\r\n"
    )


# ---------------------------------------------------------------------------
# XML: XXE and entity expansion
# ---------------------------------------------------------------------------

BILLION_LAUGHS = (
    '<!DOCTYPE OFX [<!ENTITY a "lollollollollol">'
    + "".join(f'<!ENTITY {chr(98 + i)} "{("&" + chr(97 + i) + ";") * 10}">' for i in range(9))
    + "]>\n"
)


def ofx_xxe() -> bytes:
    return ofx_xml(synth_transactions(2), prolog=XXE_PROLOG)


def ofx_billion_laughs() -> bytes:
    return ofx_xml(synth_transactions(2), prolog=BILLION_LAUGHS)


def ofx_external_dtd() -> bytes:
    return ofx_xml(
        synth_transactions(2), prolog='<!DOCTYPE OFX SYSTEM "http://169.254.169.254/latest">\n'
    )


def plain_xml_with_entity() -> bytes:
    return b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY e SYSTEM "file:///etc/passwd">]><r>&e;</r>'


def xlsx_xxe_sheet() -> bytes:
    return raw_xlsx(
        sheet_xml(
            inline_row(1, "&xxe;"),
            prolog=b'<!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>',
        )
    )


def xlsx_billion_laughs_shared_strings() -> bytes:
    sst = (
        b'<?xml version="1.0"?>'
        + BILLION_LAUGHS.replace("OFX", "sst").encode()
        + b'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        b"<si><t>&j;</t></si></sst>"
    )
    return raw_xlsx(sheet_xml(inline_row(1, "a")), extra={"xl/sharedStrings.xml": sst})


def xlsx_xxe_content_types() -> bytes:
    data = raw_xlsx(sheet_xml(inline_row(1, "a")))
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            body = src.read(info)
            if info.filename == "[Content_Types].xml":
                body = body.replace(
                    b"?>\n",
                    b'?>\n<!DOCTYPE Types [<!ENTITY x SYSTEM "file:///etc/hostname">]>\n',
                    1,
                )
            dst.writestr(info.filename, body)
    return out.getvalue()


def xlsx_bomb() -> bytes:
    return raw_xlsx(sheet_xml(b'<row r="1"></row>' + b" " * (200 * 1024 * 1024)))


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def png_header_bomb() -> bytes:
    return synth.pixel_bomb_png(100_000, 100_000)


def png_real_bomb() -> bytes:
    """A PNG that really decodes to 15000 x 15000 grey pixels (225 MP)."""
    width = height = 15_000
    raw = zlib.compressobj(9)
    row = b"\x00" + b"\x00" * width
    body = b"".join(raw.compress(row) for _ in range(height)) + raw.flush()
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + synth._png_chunk(b"IHDR", header)
        + synth._png_chunk(b"IDAT", body)
        + synth._png_chunk(b"IEND", b"")
    )


def jpeg_huge_dimensions() -> bytes:
    from PIL import Image

    jpeg = bytearray(synth.image_bytes(Image.new("L", (16, 16), 128), "JPEG"))
    sof = jpeg.index(b"\xff\xc0")
    struct.pack_into(">HH", jpeg, sof + 5, 65_500, 65_500)
    return bytes(jpeg)


def tiff_huge_dimensions() -> bytes:
    from PIL import Image

    tiff = bytearray(synth.image_bytes(Image.new("L", (16, 16), 128), "TIFF"))
    # Patch ImageWidth (256) and ImageLength (257) in the first IFD.
    (ifd,) = struct.unpack_from("<I", tiff, 4)
    (count,) = struct.unpack_from("<H", tiff, ifd)
    for n in range(count):
        entry = ifd + 2 + n * 12
        tag, kind = struct.unpack_from("<HH", tiff, entry)
        if tag in (256, 257):
            if kind == 3:
                struct.pack_into("<H", tiff, entry + 8, 65_000)
            else:
                struct.pack_into("<I", tiff, entry + 8, 1_000_000)
    return bytes(tiff)


def png_truncated() -> bytes:
    from PIL import Image

    png = synth.image_bytes(Image.new("RGB", (300, 300), (1, 2, 3)), "PNG")
    return png[: len(png) // 2]


# ---------------------------------------------------------------------------
# Mail
# ---------------------------------------------------------------------------

_HEADERS = (
    b"From: Shop <shop@example.invalid>\r\nTo: me@example.invalid\r\n"
    b"Subject: Receipt\r\nDate: Sat, 14 Mar 2026 12:45:00 +0000\r\n"
    b"Message-ID: <fuzz@example.invalid>\r\nMIME-Version: 1.0\r\n"
)


def eml_unterminated_multipart() -> bytes:
    return (
        _HEADERS
        + b'Content-Type: multipart/mixed; boundary="XYZ"\r\n\r\n--XYZ\r\n'
        + b"Content-Type: text/plain\r\n\r\nhello\r\n--XYZ\r\nContent-Type: application/pdf\r\n"
        + b"Content-Transfer-Encoding: base64\r\n\r\nJVBERi0xLjQK"
    )


def eml_deep_multipart(depth: int = 2000) -> bytes:
    body = bytearray(_HEADERS + b'Content-Type: multipart/mixed; boundary="b0"\r\n\r\n')
    for level in range(1, depth):
        body += (
            f'--b{level - 1}\r\nContent-Type: multipart/mixed; boundary="b{level}"\r\n\r\n'.encode()
        )
    body += f"--b{depth - 1}\r\nContent-Type: text/plain\r\n\r\ndeep\r\n".encode()
    for level in reversed(range(depth)):
        body += f"--b{level}--\r\n".encode()
    return bytes(body)


def eml_bad_base64() -> bytes:
    return (
        _HEADERS
        + b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        + b"--B\r\nContent-Type: text/plain\r\n\r\nhi\r\n"
        + b'--B\r\nContent-Type: application/pdf; name="a.pdf"\r\nContent-Disposition: attachment; '
        + b'filename="../../../etc/passwd.pdf"\r\nContent-Transfer-Encoding: base64\r\n\r\n'
        + b"!!!!not*base64@@@@\r\n=====\r\n--B--\r\n"
    )


def eml_header_flood() -> bytes:
    return (
        _HEADERS
        + b"X-Junk: "
        + b"a" * 2_000_000
        + b"\r\n"
        + b"X-Many: 1\r\n" * 50_000
        + b"\r\nbody\r\n"
    )


def eml_encoded_word_garbage() -> bytes:
    return (
        _HEADERS.replace(
            b"Subject: Receipt", b"Subject: =?utf-8?B?////////?= =?x-unknown?Q?=FF=FE?="
        )
        + b"Content-Type: text/html; charset=x-made-up\r\n\r\n"
        + b"<p>caf\xe9 \xff\xfe <script>bad()</script></p>\r\n"
    )


def eml_attached_message_loop() -> bytes:
    inner = EmailMessage(policy=default_policy)
    inner["From"] = "a@example.invalid"
    inner["Subject"] = "inner"
    inner["Date"] = "Sat, 14 Mar 2026 12:45:00 +0000"
    inner["Message-ID"] = "<inner@example.invalid>"
    inner.set_content("inner body")
    message = inner
    for _ in range(30):
        outer = EmailMessage(policy=default_policy)
        outer["From"] = "a@example.invalid"
        outer["Subject"] = "wrap"
        outer["Date"] = "Sat, 14 Mar 2026 12:45:00 +0000"
        outer["Message-ID"] = "<outer@example.invalid>"
        outer.set_content("wrapper")
        outer.add_attachment(message)
        message = outer
    return message.as_bytes()


def eml_with_zip_bomb_attachment() -> bytes:
    return synth.receipt_email(
        attachments=[("statement.zip", "application/zip", synth.zip_bomb(32 * 1024 * 1024))]
    )


# ---------------------------------------------------------------------------
# Disguised archives and executables
# ---------------------------------------------------------------------------


def tar_archive() -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as archive:
        member = b"Date,Amount\n2026-01-01,1.00\n"
        info = tarfile.TarInfo("statement.csv")
        info.size = len(member)
        archive.addfile(info, io.BytesIO(member))
    return out.getvalue()


def sevenzip() -> bytes:
    return b"7z\xbc\xaf\x27\x1c\x00\x04" + b"\x00" * 24 + b"payload"


def gzip_csv() -> bytes:
    import gzip

    return gzip.compress(b"Date,Amount\n2026-01-01,1.00\n")


def rar() -> bytes:
    return b"Rar!\x1a\x07\x01\x00" + b"\x00" * 64


def elf() -> bytes:
    return b"\x7fELF\x02\x01\x01" + b"\x00" * 57


def windows_pe() -> bytes:
    return b"MZ\x90\x00\x03" + b"\x00" * 59 + b"PE\x00\x00"


def csv_with_nul() -> bytes:
    return b"Date,Amount\n2026-01-01,1.00\x00\n"


def utf16_csv() -> bytes:
    return "Date,Amount\n2026-01-01,1.00\n".encode("utf-16")


# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------


def _codes(*codes: str) -> frozenset[str]:
    return frozenset(codes)


SAMPLES: Final[tuple[Sample, ...]] = (
    # -- PDF
    Sample("pdf-truncated-xref", pdf_truncated_xref, "contained"),
    Sample("pdf-wrong-startxref", pdf_wrong_startxref, "contained"),
    Sample("pdf-reference-loop", pdf_reference_loop, "extract_error", _codes("malformed_pdf")),
    Sample("pdf-indirect-length-loop", pdf_indirect_length_loop, "contained"),
    Sample("pdf-deep-nesting", pdf_deep_nesting, "extract_error", _codes("malformed_pdf")),
    Sample("pdf-stream-bomb", pdf_stream_bomb, "contained"),
    Sample(
        "pdf-javascript-launch",
        pdf_javascript,
        "ok",
        forbidden=("perfin-fuzz-js-marker", "perfin-fuzz-launch-marker"),
    ),
    Sample("pdf-huge-object-count", pdf_huge_object_count, "contained"),
    Sample("pdf-huge-mediabox", pdf_huge_mediabox, "contained"),
    Sample("pdf-encrypted", synth.encrypted_pdf, "extract_error", _codes("encrypted")),
    # -- ZIP
    Sample("zip-flat-bomb", zip_flat_bomb, "extract_error", _codes("zip_bomb", "zip_too_large")),
    # Only one level is expanded: the inner archives come back as children
    # (depth 1), which the pipeline parks as depth_limit.
    Sample("zip-nested-42", zip_nested_42, "ok"),
    Sample("zip-overlapping-members", zip_overlapping, "extract_error", _codes("zip_bomb")),
    Sample("zip-many-members", zip_many_members, "extract_error", _codes("too_many_members")),
    Sample("zip-traversal-names", zip_traversal_names, "ok", forbidden=("../", "/abs/", "C:\\")),
    Sample("zip-symlink", synth.zip_with_symlink, "ok", forbidden=("/etc/passwd",)),
    # -- polyglots
    Sample("pdf-html-polyglot", pdf_html_polyglot, "rejected", _codes("polyglot")),
    Sample("html-then-pdf", html_then_pdf, "rejected", _codes("markup")),
    Sample("png-zip-polyglot", png_zip_polyglot, "ok", forbidden=("<script",)),
    Sample("gifar", gifar, "rejected", _codes("binary_unknown")),
    Sample("jpeg-script-comment", jpeg_with_script_comment, "rejected", _codes("polyglot")),
    Sample("pdf-zip-polyglot", pdf_zip_polyglot, "contained"),
    Sample("csv-script-past-window", csv_with_script_past_window, "rejected", _codes("polyglot")),
    # -- XML
    Sample(
        "ofx-xxe",
        ofx_xxe,
        "extract_error",
        _codes("xml_forbidden", "malformed_ofx"),
        forbidden=("root:",),
    ),
    Sample(
        "ofx-billion-laughs",
        ofx_billion_laughs,
        "extract_error",
        _codes("xml_forbidden", "malformed_ofx"),
    ),
    Sample(
        "ofx-external-dtd",
        ofx_external_dtd,
        "extract_error",
        _codes("xml_forbidden", "malformed_ofx"),
    ),
    Sample("xml-not-ofx", plain_xml_with_entity, "rejected", _codes("xml_not_ofx")),
    Sample(
        "xlsx-xxe-sheet", xlsx_xxe_sheet, "extract_error", _codes("xml_dtd"), forbidden=("root:",)
    ),
    Sample(
        "xlsx-billion-laughs",
        xlsx_billion_laughs_shared_strings,
        "extract_error",
        _codes("xml_dtd"),
    ),
    Sample("xlsx-xxe-content-types", xlsx_xxe_content_types, "extract_error", _codes("xml_dtd")),
    Sample("xlsx-bomb", xlsx_bomb, "extract_error", _codes("zip_bomb", "xlsx_too_large")),
    # -- images
    Sample("png-header-bomb", png_header_bomb, "extract_error", _codes("too_many_pixels")),
    Sample("png-real-bomb", png_real_bomb, "extract_error", _codes("too_many_pixels")),
    Sample(
        "jpeg-huge-dimensions",
        jpeg_huge_dimensions,
        "extract_error",
        _codes("too_many_pixels", "malformed_image"),
    ),
    Sample("tiff-huge-dimensions", tiff_huge_dimensions, "contained"),
    Sample("png-truncated", png_truncated, "contained"),
    # -- mail
    Sample("eml-unterminated-multipart", eml_unterminated_multipart, "contained"),
    Sample("eml-deep-multipart", eml_deep_multipart, "contained"),
    Sample(
        "eml-bad-base64-traversal-name",
        eml_bad_base64,
        "contained",
        forbidden=("../", "/etc/passwd"),
    ),
    Sample("eml-header-flood", eml_header_flood, "contained"),
    Sample(
        "eml-encoded-word-garbage",
        eml_encoded_word_garbage,
        "contained",
        forbidden=("<script", "bad()"),
    ),
    Sample("eml-attached-message-loop", eml_attached_message_loop, "contained"),
    Sample("eml-zip-bomb-attachment", eml_with_zip_bomb_attachment, "contained"),
    # -- disguised
    Sample("tar-as-csv", tar_archive, "rejected", _codes("binary_unknown")),
    Sample("7z", sevenzip, "rejected", _codes("binary_unknown")),
    Sample("gzip", gzip_csv, "rejected", _codes("binary_unknown")),
    Sample("rar", rar, "rejected", _codes("binary_unknown")),
    Sample("elf", elf, "rejected", _codes("binary_unknown")),
    Sample("windows-pe", windows_pe, "rejected", _codes("binary_unknown")),
    Sample("csv-with-nul", csv_with_nul, "rejected", _codes("binary_unknown")),
    Sample("utf16-csv", utf16_csv, "rejected", _codes("text_encoding")),
)

BY_NAME: Final = {sample.name: sample for sample in SAMPLES}
