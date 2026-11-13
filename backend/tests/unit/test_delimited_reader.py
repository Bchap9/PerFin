# SPDX-License-Identifier: AGPL-3.0-or-later
"""The delimited reader on files that are valid but not strictly RFC 4180.

Two shapes real exports have and the strict reader refused or misread:
blanks left between a closing quote and the delimiter or the line end, and
cells quoted with ``'`` instead of ``"``. Reading them must not loosen what
is refused (text after a closing quote still fails the file) and must not
skip the formula guard (SECURITY.md C7). Every file here is invented.
"""

from __future__ import annotations

import csv
import random

import pytest

from app.ingestion.extract.common import ExtractFailure
from app.ingestion.extract.delimited import _reader, _tidy, extract_delimited
from app.ingestion.intake.sniff import FileType, sniff
from app.ingestion.sandbox.protocol import ExtractOptions, Table

OPTIONS = ExtractOptions()

#: Fully quoted, five columns, five lines; the last line ends with one space
#: after its closing quote and has no newline.
QUOTED_TRAILING_BLANK = (
    b'"Date","Type","Details","Amount","Balance"\r\n'
    b'"2031-03-02","deposit","Pay run 14","1800.00","2410.75"\r\n'
    b'"2031-03-05","purchase","Corner Bakery, Elm St","-6.40","2404.35"\r\n'
    b'"2031-03-09","purchase","Tram pass","-48.00","2356.35"\r\n'
    b'"2031-03-11","interest","Monthly interest","0.15","12.50" '
)

#: Unquoted header, then rows whose every cell is written 'like this'.
SINGLE_QUOTED = (
    b"date;comment;sign;amount;category\n"
    b"'01/08/2008';'CARD 1';'Withdrawal';'9.3';'Food'\n"
    b"'03/08/2008';'Lunch; two people';'Withdrawal';'21.75';'Food'\n"
    b"'07/08/2008';'Rent ''August''';'Withdrawal';'640';'Home'\n"
    b"'09/08/2008';'Refund';'Deposit';'12.1';'Other'\n"
)

APOSTROPHES = (
    b"Date,Payee,Amount\n"
    b"2031-04-01,O'Brien Hardware,-19.99\n"
    b"2031-04-02,'s-Gravenhof Market,-7.25\n"
    b"2031-04-03,Miller's Dairy,-3.10\n"
    b"2031-04-04,Night Owl 'til Dawn,-11.00\n"
)

#: Unquoted rows that open with a text-forced code and end with a length in
#: feet: each row starts and ends with an apostrophe, and no cell is quoted.
BRACKETED_ROWS = (
    b"Code,Item,Length\n"
    b"'00417,Cedar plank,8'\n"
    b"'00418,Copper pipe,12'\n"
    b"'00419,Guard rail,6'\n"
    b"'00420,Oak beam,10'\n"
)


def _table(data: bytes, kind: str = "csv") -> Table:
    result = extract_delimited(data, OPTIONS, kind)
    assert result.ok
    return result.tables[0]


def _sniffed_table(data: bytes) -> Table:
    """The table as the pipeline reads it: sniffed, then read as the sniffed kind."""
    sniffed = sniff(data)
    assert isinstance(sniffed, FileType), sniffed
    kind = sniffed.extract_kind
    assert kind in ("csv", "tsv"), sniffed
    return _table(data, kind)


# ---------------------------------------------------------------------------
# Blanks after a closing quote
# ---------------------------------------------------------------------------


def test_blank_after_closing_quote_is_read() -> None:
    table = _table(QUOTED_TRAILING_BLANK)
    assert table.delimiter == ","
    assert len(table.rows) == 5
    assert all(len(row) == 5 for row in table.rows)
    assert table.rows[-1][-1] == "12.50"
    assert table.rows[2][2] == "Corner Bakery, Elm St"
    assert table.has_header


@pytest.mark.parametrize("blank", [" ", "\t", " \t "])
def test_blank_between_closing_quote_and_delimiter_is_read(blank: str) -> None:
    header = f'"Date"{blank},"Payee"{blank},"Amount"\n'
    text = header + f'"2031-05-01"{blank},"Kiosk, north"{blank},"-2.00"{blank}\n'
    table = _table(text.encode())
    assert table.rows == [["Date", "Payee", "Amount"], ["2031-05-01", "Kiosk, north", "-2.00"]]


@pytest.mark.parametrize("blank", [" ", "\t", " \t "])
@pytest.mark.parametrize("line_end_blank", [False, True])
def test_blank_before_the_delimiter_is_read_from_sniff_to_table(
    blank: str, line_end_blank: bool
) -> None:
    """A tab after each closing quote gives every line the tab count of a TSV file."""
    last = blank if line_end_blank else ""
    lines = [
        f'"Date"{blank},"Payee"{blank},"Amount"{last}',
        f'"2031-05-01"{blank},"Kiosk, north"{blank},"-2.00"{last}',
        f'"2031-05-02"{blank},"Kiosk, south"{blank},"-3.00"{last}',
        f'"2031-05-03"{blank},"Ferry pier"{blank},"-4.50"{last}',
    ]
    table = _sniffed_table(("\n".join(lines) + "\n").encode())
    assert table.delimiter == ","
    assert table.rows == [
        ["Date", "Payee", "Amount"],
        ["2031-05-01", "Kiosk, north", "-2.00"],
        ["2031-05-02", "Kiosk, south", "-3.00"],
        ["2031-05-03", "Ferry pier", "-4.50"],
    ]


def test_blank_before_a_semicolon_is_read_from_sniff_to_table() -> None:
    data = (
        b'"Date"\t;"Payee"\t;"Amount"\n'
        b'"2031-05-01"\t;"Kiosk; north"\t;"-2,00"\n'
        b'"2031-05-02"\t;"Ferry pier"\t;"-4,50"\n'
    )
    table = _sniffed_table(data)
    assert table.delimiter == ";"
    assert table.rows[1] == ["2031-05-01", "Kiosk; north", "-2,00"]
    assert table.column_count == 3


def test_tab_separated_file_with_a_few_delimiter_led_cells_stays_tab_separated() -> None:
    """One memo that opens with a comma and a quote does not make the tabs blanks."""
    data = (
        b"Date\tMemo\tAmount\n"
        b'2031-05-01\t,"odd" memo\t-2.00\n'
        b"2031-05-02\tplain memo\t-3.00\n"
        b"2031-05-03\tanother memo\t-4.50\n"
    )
    table = _sniffed_table(data)
    assert table.delimiter == "\t"
    assert table.rows[1] == ["2031-05-01", ',"odd" memo', "-2.00"]
    assert table.column_count == 3


def test_tab_separated_file_whose_tabs_are_not_blanks_is_read_with_tabs() -> None:
    """Cells that open with a comma and a quote, but no other delimiter reads as a table."""
    data = b'"a"\t,"b" x\n"c"\t,"d" y,z\n'
    table = _table(data, "tsv")
    assert table.delimiter == "\t"
    assert table.rows == [["a", ',"b" x'], ["c", ',"d" y,z']]


def test_blank_inside_quotes_is_kept() -> None:
    table = _table(b'"Date","Payee","Amount"\n"2031-05-01"," Kiosk ","-2.00" \n')
    assert table.rows[1] == ["2031-05-01", " Kiosk ", "-2.00"]


def test_quote_then_blank_inside_a_quoted_cell_is_kept() -> None:
    # The doubled quote is content, so the blank and comma after it are too.
    table = _table(b'"Date","Payee","Amount"\n"2031-05-01","the ""Kiosk"" ,north","-2.00"\n')
    assert table.rows[1] == ["2031-05-01", 'the "Kiosk" ,north', "-2.00"]


def test_blank_of_an_unquoted_cell_is_kept() -> None:
    table = _table(b'Date,Payee,Amount\n2031-05-01,Kiosk ,"-2.00" \n')
    assert table.rows[1] == ["2031-05-01", "Kiosk ", "-2.00"]


def test_tabs_of_a_tab_separated_file_are_cells_not_blanks() -> None:
    table = _table(b'"Date"\t"Memo"\t"Amount"\n"2031-05-01" \t\t"-2.00" \n', "tsv")
    assert table.rows[1] == ["2031-05-01", "", "-2.00"]


def test_blank_after_quote_with_spaced_fields() -> None:
    table = _table(b'Date, Payee, Amount\n2031-05-01, "Kiosk, north" , "-2.00" \n')
    assert table.rows[1] == ["2031-05-01", "Kiosk, north", "-2.00"]


@pytest.mark.parametrize(
    "data",
    [
        b'"Date","Payee","Amount"\n"2031-05-01","Kiosk" x,"-2.00"\n',
        b'"Date","Payee","Amount"\n"2031-05-01","Kiosk" "north","-2.00"\n',
        b'"Date","Payee","Amount"\n"2031-05-01","Kiosk"x ,"-2.00"\n',
    ],
)
def test_text_after_a_closing_quote_still_fails_the_file(data: bytes) -> None:
    with pytest.raises(ExtractFailure) as caught:
        extract_delimited(data, OPTIONS, "csv")
    assert caught.value.code in ("malformed_csv", "no_delimiter")


def _strict_rows(text: str, delimiter: str, quote: str, skip_space: bool) -> list[list[str]] | None:
    try:
        return list(_reader(text, delimiter, skip_space=skip_space, quote=quote))
    except csv.Error:
        return None


@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("skip_space", [False, True])
def test_a_file_the_strict_reader_reads_is_read_the_same(quote: str, skip_space: bool) -> None:
    """Dropping blanks after closing quotes never changes a file that already reads."""
    rng = random.Random(20311)
    read = 0
    for _ in range(20_000):
        delimiter = rng.choice(",;|\t")
        pieces = ["a", "b", " ", "\t", "\n", "\r\n", "'", '"', quote, quote, delimiter, delimiter]
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 14)))
        before = _strict_rows(text, delimiter, quote, skip_space)
        if before is None:
            continue
        read += 1
        tidied = _tidy(text, delimiter, quote, skip_space=skip_space)
        assert _strict_rows(tidied, delimiter, quote, skip_space) == before, repr(text)
    assert read > 5_000


# ---------------------------------------------------------------------------
# Single-quoted cells
# ---------------------------------------------------------------------------


def test_single_quoted_cells_are_unquoted() -> None:
    table = _table(SINGLE_QUOTED)
    assert table.delimiter == ";"
    assert table.column_count == 5
    assert table.rows[1] == ["01/08/2008", "CARD 1", "Withdrawal", "9.3", "Food"]
    assert table.rows[2][1] == "Lunch; two people"  # the delimiter inside a quoted cell
    assert table.rows[3][1] == "Rent 'August'"  # a doubled quote is one quote
    assert len(table.rows) == 5
    assert table.has_header


def test_single_quoted_cells_with_trailing_blanks_are_unquoted() -> None:
    data = SINGLE_QUOTED.replace(b"';'", b"' ;'").rstrip(b"\n") + b" "
    table = _table(data)
    assert table.rows[1] == ["01/08/2008", "CARD 1", "Withdrawal", "9.3", "Food"]
    assert table.rows[4] == ["09/08/2008", "Refund", "Deposit", "12.1", "Other"]


def test_formula_in_a_single_quoted_cell_is_neutralised() -> None:
    data = SINGLE_QUOTED.replace(b"'CARD 1'", b"'=HYPERLINK(\"x\")'").replace(
        b"'Refund'", b"'@SUM(A1:A2)'"
    )
    table = _table(data)
    assert table.rows[1][1] == '\'=HYPERLINK("x")'
    assert table.rows[4][1] == "'@SUM(A1:A2)"
    assert table.rows[1][3] == "9.3"


def test_text_after_a_closing_single_quote_is_not_read_as_quoted() -> None:
    # The file does not read with ' as its quote, so it is read as written.
    data = SINGLE_QUOTED.replace(b"'CARD 1'", b"'CARD' 1")
    table = _table(data)
    assert table.rows[1][0] == "'01/08/2008'"
    assert table.rows[1][1] == "'CARD' 1"


def test_apostrophe_in_text_is_not_a_quote() -> None:
    table = _table(APOSTROPHES)
    assert table.rows == [
        ["Date", "Payee", "Amount"],
        ["2031-04-01", "O'Brien Hardware", "-19.99"],
        ["2031-04-02", "'s-Gravenhof Market", "-7.25"],
        ["2031-04-03", "Miller's Dairy", "-3.10"],
        ["2031-04-04", "Night Owl 'til Dawn", "-11.00"],
    ]


def test_a_few_single_quoted_cells_in_a_plain_file_stay_as_written() -> None:
    data = b"Date,Payee,Amount\n2031-04-01,'Quoted Name',-19.99\n2031-04-02,Plain Name,-7.25\n"
    table = _table(data)
    assert table.rows[1] == ["2031-04-01", "'Quoted Name'", "-19.99"]


@pytest.mark.parametrize("kind", ["csv", "tsv"])
def test_rows_bracketed_by_apostrophes_are_read_as_written(kind: str) -> None:
    """An apostrophe that opens a row and one that closes it are not a quoted cell."""
    data = BRACKETED_ROWS if kind == "csv" else BRACKETED_ROWS.replace(b",", b"\t")
    table = _table(data, kind)
    assert table.column_count == 3
    assert table.rows == [
        ["Code", "Item", "Length"],
        ["'00417", "Cedar plank", "8'"],
        ["'00418", "Copper pipe", "12'"],
        ["'00419", "Guard rail", "6'"],
        ["'00420", "Oak beam", "10'"],
    ]


@pytest.mark.parametrize(
    ("data", "rows"),
    [
        (
            b"Note,Paid,Shop\n"
            b"'-VOID,4.00,Weavers'\n"
            b"'-VOID,5.00,Tanners'\n"
            b"'-VOID,6.00,Glaziers'\n"
            b"'-VOID,7.00,Thatchers'\n",
            [
                ["Note", "Paid", "Shop"],
                ["'-VOID", "4.00", "Weavers'"],
                ["'-VOID", "5.00", "Tanners'"],
                ["'-VOID", "6.00", "Glaziers'"],
                ["'-VOID", "7.00", "Thatchers'"],
            ],
        ),
        # No header: the first line opens with an apostrophe and the last one ends with one.
        (b"'til dusk,-3.00\n14,cats'\n", [["'til dusk", "-3.00"], ["14", "cats'"]]),
    ],
)
def test_apostrophes_at_both_ends_of_a_record_are_text(data: bytes, rows: list[list[str]]) -> None:
    assert _table(data).rows == rows


def test_single_quotes_are_not_read_when_the_table_is_better_without() -> None:
    """Most cells are written 'like this', but only the plain reading keeps every row's shape."""
    data = b"Left,Right\n'a','b'\n'c','d'\n'e','f'\n'g','h'\n'i,j'\n'k,l'\n"
    table = _table(data)
    assert table.column_count == 2
    assert table.rows[1] == ["'a'", "'b'"]
    assert table.rows[5] == ["'i", "j'"]


def test_single_quoted_cells_that_hold_the_delimiter_are_unquoted() -> None:
    """The delimiter inside most cells does not stop the single-quote reading."""
    data = (
        b"'Date','Payee','Amount'\n"
        b"'2031-06-01','Pier, east','4.10'\n"
        b"'2031-06-02','Pier, west','5.20'\n"
        b"'2031-06-03','Depot','6.30'\n"
    )
    table = _table(data)
    assert table.rows[1] == ["2031-06-01", "Pier, east", "4.10"]
    assert table.column_count == 3


def test_single_quoted_cells_over_several_lines_are_unquoted() -> None:
    """Read as text, each broken cell adds a row: more rows, but less of a table."""
    data = (
        b"date;memo;amount\n"
        b"'01/09/2031';'Order 17\nleft at the gate';'4.10'\n"
        b"'02/09/2031';'Order 18\nleft at the door';'5.20'\n"
        b"'03/09/2031';'Order 19\nleft at the desk';'6.30'\n"
    )
    table = _table(data)
    assert table.rows == [
        ["date", "memo", "amount"],
        ["01/09/2031", "Order 17\nleft at the gate", "4.10"],
        ["02/09/2031", "Order 18\nleft at the door", "5.20"],
        ["03/09/2031", "Order 19\nleft at the desk", "6.30"],
    ]


def test_double_quoted_file_keeps_its_apostrophes() -> None:
    data = (
        b'"Date","Payee","Amount"\n'
        b'"2031-04-01","\'Quoted\'","-19.99"\n'
        b'"2031-04-02","O\'Brien","-7.25"\n'
    )
    table = _table(data)
    assert table.rows[1] == ["2031-04-01", "'Quoted'", "-19.99"]
    assert table.rows[2] == ["2031-04-02", "O'Brien", "-7.25"]
