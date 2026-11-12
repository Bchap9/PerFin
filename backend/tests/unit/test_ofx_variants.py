# SPDX-License-Identifier: AGPL-3.0-or-later
"""OFX files whose wrapping is not what the spec says (synthetic files).

A real exporter's declaration is often wrong: no ``<?xml ...?>`` before
``<?OFX ...?>``, an encoding name no XML parser knows, an XML declaration over
an SGML body. Such files are read; DTDs, entities and any other markup
declaration or processing instruction stay refused (SECURITY.md C6).
"""

from __future__ import annotations

import pytest

from app.ingestion.extract.common import ExtractFailure
from app.ingestion.extract.ofx import extract_ofx
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult
from app.ingestion.sandbox.runner import run_extraction
from tests.fixtures.statements import BILLION_LAUGHS_PROLOG, XXE_PROLOG
from tests.fixtures.synth_ofx_variants import (
    OFX_INSTRUCTION,
    ROWS,
    SGML_HEADER,
    XML_DECLARATION,
    Row,
    ofx2_unknown_encoding,
    ofx2_without_xml_declaration,
    sgml_body,
    sgml_double_escaped_payee,
    xml_body,
    xml_declaration_over_sgml_body,
)

OPTIONS = ExtractOptions()
XML_UTF8 = XML_DECLARATION.format(encoding="UTF-8")
STYLESHEET = '<?xml-stylesheet type="text/xsl" href="statement.xsl"?>\n'
DOCTYPE = '<!DOCTYPE OFX [<!ENTITY x "y">]>\n'


def _rows(result: ExtractResult) -> list[Row]:
    assert result.ok, result.error
    [statement] = result.statements
    return [(t.posted, t.amount, t.payee or "") for t in statement.transactions]


def _read(data: bytes) -> list[Row]:
    return _rows(extract_ofx(data, OPTIONS, "ofx"))


def _refusal(data: bytes) -> str:
    with pytest.raises(ExtractFailure) as caught:
        extract_ofx(data, OPTIONS, "ofx")
    return caught.value.code


def test_ofx2_without_xml_declaration_is_read() -> None:
    data = ofx2_without_xml_declaration()
    assert data.startswith(b"<?OFX OFXHEADER=")
    assert _read(data) == ROWS


@pytest.mark.parametrize("encoding", ["USASCII", "utf-16"])
def test_unknown_declared_encoding_is_read(encoding: str) -> None:
    data = ofx2_unknown_encoding(encoding)
    assert f'encoding="{encoding}"'.encode() in data
    assert _read(data) == ROWS


@pytest.mark.parametrize("encoding", ["USASCII", "utf-16"])
def test_unknown_declared_encoding_does_not_crash_the_sandbox_child(encoding: str) -> None:
    result = run_extraction("ofx", ofx2_unknown_encoding(encoding))
    assert result.error is None or result.error.code != "extractor_crashed"
    assert _rows(result) == ROWS


def test_sgml_body_under_xml_declaration_is_read() -> None:
    data = xml_declaration_over_sgml_body()
    assert data.startswith(b"<?xml ")
    assert b"<TRNAMT>-6.25\n" in data  # a leaf with no end tag: not XML
    assert _read(data) == ROWS


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(SGML_HEADER + STYLESHEET + sgml_body(), id="sgml-stylesheet"),
        pytest.param(SGML_HEADER + DOCTYPE + sgml_body(), id="sgml-doctype"),
        pytest.param(OFX_INSTRUCTION + STYLESHEET + sgml_body(), id="ofx-pi-stylesheet"),
        pytest.param(XML_UTF8 + OFX_INSTRUCTION + STYLESHEET + sgml_body(), id="xml-stylesheet"),
        pytest.param(
            XML_UTF8 + OFX_INSTRUCTION + sgml_body().replace("<CURDEF>", f"{DOCTYPE}<CURDEF>"),
            id="xml-doctype-in-body",
        ),
        pytest.param(
            XML_UTF8 + OFX_INSTRUCTION + sgml_body().replace("<CURDEF>", f"{XML_UTF8}<CURDEF>"),
            id="xml-second-declaration",
        ),
        pytest.param(
            XML_UTF8 + OFX_INSTRUCTION + sgml_body().replace("Quill", "<![CDATA[Quill]]>"),
            id="xml-cdata",
        ),
    ],
)
def test_other_processing_instructions_stay_refused(data: str) -> None:
    assert _refusal(data.encode("ascii")) == "markup_declaration_forbidden"


@pytest.mark.parametrize("prolog", [XXE_PROLOG, BILLION_LAUGHS_PROLOG], ids=["xxe", "laughs"])
@pytest.mark.parametrize("declaration", ["", XML_UTF8], ids=["ofx-pi-only", "xml-declaration"])
@pytest.mark.parametrize("body", [xml_body(), sgml_body()], ids=["xml-body", "sgml-body"])
def test_a_dtd_is_refused_whatever_the_declaration_and_body(
    prolog: str, declaration: str, body: str
) -> None:
    """A DTD is never skipped over: not by the new ``<?OFX`` route, not by the fallback."""
    payload = body.replace("Quill Stationers", "&xxe;")
    data = (declaration + OFX_INSTRUCTION + prolog + payload).encode("ascii")
    assert _refusal(data) == "xml_forbidden"


def test_xml_that_is_not_ofx_stays_refused() -> None:
    data = (XML_UTF8 + "<statement><row>1</row></statement>\n").encode("ascii")
    assert _refusal(data) == "malformed_ofx"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("<statement><row>1</statement>\n", id="no-ofx-at-all"),
        # The tokenizer finds no ``<OFX>`` here: the refusal names the XML, not it.
        pytest.param('<OFX release="2"><row>1</OFX>\n', id="ofx-root-with-attribute"),
    ],
)
def test_broken_xml_the_tokenizer_cannot_read_either_is_malformed_xml(body: str) -> None:
    assert _refusal((XML_UTF8 + body).encode("ascii")) == "malformed_xml"


@pytest.mark.parametrize("body", [xml_body(), sgml_body()], ids=["xml-body", "sgml-body"])
def test_an_ofx2_file_cut_short_is_not_read_by_the_fallback(body: str) -> None:
    """Without ``</OFX>`` the tokenizer would import half an amount as if it were whole."""
    cut = body[: body.index("-42.18") + len("-42.1")]
    data = (XML_UTF8 + OFX_INSTRUCTION + cut).encode("ascii")
    assert _refusal(data) == "malformed_xml"


def test_double_escaped_entity_is_unescaped_once_more() -> None:
    data = sgml_double_escaped_payee()
    assert b"<NAME>A &amp;lt;B&amp;gt;\n" in data
    rows = _read(data)
    assert rows[0][2] == "A <B>"
    assert rows[1:] == ROWS[1:]


def test_single_escaped_entities_are_unchanged() -> None:
    data = (SGML_HEADER + sgml_body()).replace("Harbor Lane Bakery", "Salt &amp; Rye &lt;Deli&gt;")
    assert _read(data.encode("ascii"))[0][2] == "Salt & Rye <Deli>"


def test_unescaping_stops_after_two_more_passes() -> None:
    data = (SGML_HEADER + sgml_body()).replace("Harbor Lane Bakery", "A &amp;amp;amp;amp;lt;B")
    assert _read(data.encode("ascii"))[0][2] == "A &amp;lt;B"
