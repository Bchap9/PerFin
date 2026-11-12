# SPDX-License-Identifier: AGPL-3.0-or-later
"""OFX files whose wrapping is not what the spec says (synthetic files).

A real exporter's declaration is often wrong: no ``<?xml ...?>`` before
``<?OFX ...?>``, an encoding name no XML parser knows, an XML declaration over
an SGML body. Such files are read; DTDs, entities and any other markup
declaration or processing instruction stay refused (SECURITY.md C6).

Nor does every record sit where the spec puts it: leaves printed with no
value, a transaction after a stray end of the list, an amount that is not a
number. What can be read is read, and every record that is not is counted in
a ``transaction_skipped:<n>`` warning, one per statement.
"""

from __future__ import annotations

import pytest

from app.ingestion.extract.common import ExtractFailure
from app.ingestion.extract.ofx import extract_ofx, parse_sgml, skipped_warnings
from app.ingestion.sandbox.protocol import (
    MAX_STATEMENTS,
    WARNINGS_MAX,
    ExtractOptions,
    ExtractResult,
)
from app.ingestion.sandbox.runner import run_extraction
from tests.fixtures.statements import BILLION_LAUGHS_PROLOG, XXE_PROLOG
from tests.fixtures.synth_ofx_variants import (
    OFX_INSTRUCTION,
    ROWS,
    SGML_HEADER,
    STRAY_END_ROWS,
    UNREADABLE_ROW,
    VALUELESS_ROWS,
    XML_DECLARATION,
    Row,
    ofx2_unknown_encoding,
    ofx2_without_xml_declaration,
    sgml_body,
    sgml_double_escaped_payee,
    sgml_with_stray_list_end,
    sgml_with_unreadable_amount,
    sgml_with_valueless_leaves,
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


def _skips(result: ExtractResult) -> list[str]:
    """The skipped-record warnings, and no other spelling of them."""
    return [warning for warning in result.warnings if "skipped" in warning]


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


# ---------------------------------------------------------------------------
# Records the reader used to lose
# ---------------------------------------------------------------------------

#: A brokerage statement with two transfers, the second of zero units (unreadable).
INVESTMENT_WITH_AN_EMPTY_TRANSFER = (
    SGML_HEADER + "<OFX><INVSTMTMSGSRSV1><INVSTMTTRNRS><INVSTMTRS>"
    "<CURDEF>USD<INVACCTFROM><BROKERID>broker.example<ACCTID>55443322</INVACCTFROM>"
    "<INVTRANLIST><DTSTART>20260401<DTEND>20260430"
    "<TRANSFER><INVTRAN><FITID>SYN-T1<DTTRADE>20260410</INVTRAN>"
    "<SECID><UNIQUEID>ZZ0000D41<UNIQUEIDTYPE>CUSIP</SECID>"
    "<UNITS>12<TFERACTION>IN<POSTYPE>LONG</TRANSFER>"
    "<TRANSFER><INVTRAN><FITID>SYN-T2<DTTRADE>20260412</INVTRAN>"
    "<SECID><UNIQUEID>ZZ0000D41<UNIQUEIDTYPE>CUSIP</SECID>"
    "<UNITS>0<TFERACTION>IN<POSTYPE>LONG</TRANSFER>"
    "</INVTRANLIST></INVSTMTRS></INVSTMTTRNRS></INVSTMTMSGSRSV1></OFX>\n"
).encode("ascii")


def _statement_block(body: str) -> str:
    """The ``<BANKMSGSRSV1>`` ... ``</BANKMSGSRSV1>`` of an OFX body."""
    end = "</BANKMSGSRSV1>\n"
    return body[body.index("<BANKMSGSRSV1>") : body.index(end) + len(end)]


def _repeated(body: str, times: int) -> bytes:
    """``body`` with its one statement printed ``times`` times."""
    block = _statement_block(body)
    return (SGML_HEADER + body.replace(block, block * times)).encode("ascii")


def test_valueless_sgml_leaves_do_not_swallow_fields() -> None:
    data = sgml_with_valueless_leaves()
    assert b"<TRNTYPE>\n<DTPOSTED>" in data  # the date follows a tag with no value
    assert b"</TRNTYPE>" not in data
    result = extract_ofx(data, OPTIONS, "ofx")
    [statement] = result.statements
    assert [(t.posted, t.amount, t.memo) for t in statement.transactions] == VALUELESS_ROWS
    assert all(t.payee is None and t.fitid is None for t in statement.transactions)
    assert all(t.type is None for t in statement.transactions)
    assert _skips(result) == []


def test_transactions_outside_the_list_are_read() -> None:
    data = sgml_with_stray_list_end()
    assert data.count(b"</BANKTRANLIST>") == 2
    result = extract_ofx(data, OPTIONS, "ofx")
    assert _rows(result) == STRAY_END_ROWS
    assert _skips(result) == []


def test_transactions_of_a_second_list_are_read() -> None:
    body = sgml_body()
    lists = body.replace(
        "</STMTTRN>\n<STMTTRN>", "</STMTTRN>\n</BANKTRANLIST><BANKTRANLIST>\n<STMTTRN>", 1
    )
    assert lists.count("<BANKTRANLIST>") == 2
    result = extract_ofx((SGML_HEADER + lists).encode("ascii"), OPTIONS, "ofx")
    assert _rows(result) == ROWS
    assert _skips(result) == []


def test_unreadable_record_is_counted() -> None:
    data = sgml_with_unreadable_amount()
    assert b"<TRNAMT>abc\n" in data
    result = extract_ofx(data, OPTIONS, "ofx")
    assert _rows(result) == [row for index, row in enumerate(ROWS) if index != UNREADABLE_ROW]
    assert _skips(result) == ["transaction_skipped:1"]


def test_a_record_without_a_date_is_counted() -> None:
    body = sgml_body().replace("<DTPOSTED>20260303\n", "", 1)
    result = extract_ofx((SGML_HEADER + body).encode("ascii"), OPTIONS, "ofx")
    assert _rows(result) == ROWS[1:]
    assert _skips(result) == ["transaction_skipped:1"]


def test_a_file_that_loses_no_record_has_no_skip_warning() -> None:
    for body in (SGML_HEADER + sgml_body(), XML_UTF8 + OFX_INSTRUCTION + xml_body()):
        result = extract_ofx(body.encode("ascii"), OPTIONS, "ofx")
        assert _rows(result) == ROWS
        assert result.warnings == []


def test_each_statement_counts_its_own_skipped_records() -> None:
    """Two statements that lose one record each: two warnings, not one of a set."""
    body = sgml_with_unreadable_amount().decode("ascii").removeprefix(SGML_HEADER)
    result = extract_ofx(_repeated(body, 2), OPTIONS, "ofx")
    assert [len(statement.transactions) for statement in result.statements] == [2, 2]
    assert _skips(result) == ["transaction_skipped:1", "transaction_skipped:1"]


def test_a_statement_with_every_record_unreadable_counts_them_all() -> None:
    body = sgml_body()
    for _, amount, _ in ROWS:
        body = body.replace(f"<TRNAMT>{amount}\n", "<TRNAMT>n/a\n")
    result = extract_ofx((SGML_HEADER + body).encode("ascii"), OPTIONS, "ofx")
    assert _rows(result) == []
    assert _skips(result) == [f"transaction_skipped:{len(ROWS)}"]


def test_transactions_the_reader_cannot_reach_are_counted() -> None:
    """No ``</STMTTRN>`` anywhere: each transaction nests in the one before it.

    ``STMTTRN`` is a known aggregate, so it still nests without its end tag.
    The first is read; the two inside it are not, and are not lost in silence.
    """
    body = sgml_body().replace("</STMTTRN>\n", "")
    assert "</STMTTRN>" not in body
    result = extract_ofx((SGML_HEADER + body).encode("ascii"), OPTIONS, "ofx")
    assert _rows(result) == ROWS[:1]
    assert _skips(result) == ["transaction_skipped:2"]


def test_aggregate_with_end_tag_is_still_an_aggregate() -> None:
    root = parse_sgml(sgml_body())
    [statement] = root.iter("STMTRS")
    assert [node.name for node in statement.children] == [
        "CURDEF",
        "BANKACCTFROM",
        "BANKTRANLIST",
        "LEDGERBAL",
    ]
    tranlist = statement.child("BANKTRANLIST")
    assert tranlist is not None
    assert [node.name for node in tranlist.children] == ["DTSTART", "DTEND", *["STMTTRN"] * 3]
    for transaction, (_, amount, payee) in zip(tranlist.children[2:], ROWS, strict=True):
        assert [node.name for node in transaction.children] == [
            "TRNTYPE",
            "DTPOSTED",
            "TRNAMT",
            "FITID",
            "NAME",
        ]
        assert all(not leaf.children for leaf in transaction.children)
        assert transaction.text("TRNAMT") == str(amount)
        assert transaction.text("NAME") == payee
    assert _read((SGML_HEADER + sgml_body()).encode("ascii")) == ROWS


def test_an_empty_tag_with_an_end_tag_elsewhere_is_an_aggregate() -> None:
    """``<PAYEE>`` has no value and is closed: its ``<NAME>`` stays inside it."""
    body = sgml_body().replace(
        "<NAME>Harbor Lane Bakery\n", "<PAYEE>\n<NAME>Harbor Lane Bakery\n</PAYEE>\n"
    )
    root = parse_sgml(body)
    first = next(root.iter("STMTTRN"))
    assert first.child("NAME") is None
    assert first.text("PAYEE", "NAME") == "Harbor Lane Bakery"
    assert _read((SGML_HEADER + body).encode("ascii")) == ROWS


def test_an_unknown_empty_tag_with_no_end_tag_is_a_leaf() -> None:
    body = sgml_body().replace("<DTPOSTED>", "<X.VENDORNOTE>\n<DTPOSTED>")
    root = parse_sgml(body)
    for transaction in root.iter("STMTTRN"):
        note = transaction.child("X.VENDORNOTE")
        assert note is not None
        assert note.value is None
        assert note.children == []
    assert _read((SGML_HEADER + body).encode("ascii")) == ROWS


def test_investment_activity_that_cannot_be_read_is_counted() -> None:
    result = extract_ofx(INVESTMENT_WITH_AN_EMPTY_TRANSFER, OPTIONS, "ofx")
    [statement] = result.investments
    [transfer] = statement.activity
    assert transfer.type == "transfer_in"
    assert transfer.quantity == 12
    assert _skips(result) == ["transaction_skipped:1"]


@pytest.mark.parametrize(
    ("counts", "room", "expected"),
    [
        ([], 5, []),
        ([0, 2, 0, 1], 5, ["transaction_skipped:2", "transaction_skipped:1"]),
        ([1, 2, 3], 3, ["transaction_skipped:1", "transaction_skipped:2", "transaction_skipped:3"]),
        ([1, 2, 3, 4], 2, ["transaction_skipped:1", "transaction_skipped:9"]),
        ([1, 2, 3], 1, ["transaction_skipped:6"]),
        ([1, 2, 3], 0, ["transaction_skipped:6"]),
    ],
)
def test_skip_warnings_fold_into_the_room_left_and_keep_their_total(
    counts: list[int], room: int, expected: list[str]
) -> None:
    assert skipped_warnings(counts, room) == expected


def test_more_skipping_statements_than_warnings_fit_keep_the_total() -> None:
    """One warning per statement would overflow the result; the total must survive."""
    assert MAX_STATEMENTS >= WARNINGS_MAX
    body = sgml_with_unreadable_amount().decode("ascii").removeprefix(SGML_HEADER)
    result = extract_ofx(_repeated(body, MAX_STATEMENTS + 1), OPTIONS, "ofx")
    assert len(result.statements) == MAX_STATEMENTS
    assert f"statements_truncated:{MAX_STATEMENTS}" in result.warnings
    assert len(result.warnings) == WARNINGS_MAX
    counts = [int(w.rpartition(":")[2]) for w in _skips(result)]
    assert sum(counts) == MAX_STATEMENTS
    assert ExtractResult.model_validate(result.model_dump(mode="json")) == result
