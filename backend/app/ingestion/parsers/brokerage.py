# SPDX-License-Identifier: AGPL-3.0-or-later
"""Brokerage statements: holdings and activity sections, and their reconciliation.

Two halves (INGESTION.md "Brokerage statements", SECURITY.md C12):

* :func:`reconcile_investment` checks an :class:`InvestmentStatement` from
  any source (an OFX ``INVSTMTRS`` from the sandbox, a PDF read here):

  - **positions**: quantity x unit price = market value for every position
    that prints all three, within half a unit of the price's last printed
    decimal times the quantity, plus :data:`~app.ingestion.reconcile.TOLERANCE`;
  - **holdings_total**: the positions' market values add up to the
    holdings total the statement prints;
  - **cash**: opening cash + the activity's cash effects = closing cash
    (a statement printing no closing cash: the account value less the
    holdings is the closing cash, and the next check is not run);
  - **total_value**: holdings + closing cash = the account value printed;
  - **index_value** (a document of several accounts): the account value
    printed on the account's own pages = the ending value the document's
    account index prints for it. An account that prints no value of its own
    takes the index's.

  A position quoted per 100 of face value (``price_per_100``) is checked as
  quantity x price / 100.

  Status ``mismatch`` when any check fails, ``unavailable`` when none could
  run, else ``ok``.

* :func:`section_checks` and :func:`localise` say **where** a failed check
  comes from, from what the statement prints about itself: the ``Total
  ...`` line under each activity section (:func:`_total_scope` says which
  rows it sums) and the cash-flow summary restating every section. Only
  the rows no such figure vouches for carry the ``reconciliation`` doubt
  (``InvestmentReconciliation.doubtful_activity`` / ``doubtful_positions``);
  the importer (``app.domain.investments.statement_import``) writes the
  rest of the statement and never a row in doubt. A statement from a
  source that says nothing of the kind (OFX, an activity export) is held
  whole, as before.

* :class:`BrokerageStatementParser` reads the stored pages of a PDF or
  scan (the words and boxes the sandbox extracted; never file bytes,
  ADR-0006) in the worker:

  - **accounts**: one statement per account when the pages name several
    (:mod:`app.ingestion.parsers.accounts`: account headings such as
    ``Account # Z12-345678`` or ``INDIVIDUAL - TOD Z12-345678`` on each
    account's pages, also beside an amount at the top of a page, and the
    account index's names and types), each with its own last four digits,
    name, type, pages, tables, totals and reconciliation, and a bank
    account among them (a combined statement's checking) read as a bank
    statement; a document naming one account is read as one statement
    (the parser's class doc);
  - a **holdings** table: a header naming a quantity, a market value and a
    symbol or security name (optional price, cost basis, beginning market
    value; columns such as unrealized gain/loss, estimated income, maturity
    or coupon are recognised and not read, and so is any header cell naming
    no known column, so its values never slide into a neighbour), repeated
    per page. A header may run over up to three lines (``Price`` / ``Per
    Unit``, ``Ending Market Value`` / ``Accrued Interest (AI)``, a date under
    ``Ending Market Value``): the line with the most cells gives the columns
    and the words of the others join the column they lie over; a heading
    (an activity section, an asset group) is never part of a header, and a
    header cell whose words are several known headers (``Transaction
    Description Quantity``) is split into them (:func:`split_header_cell`).
    Rows may be grouped under asset-class headings (:data:`ASSET_GROUPS`:
    *Mutual Funds*, *Stocks*, *Corporate Bonds*, ...; also printed above
    the table's header, ``Core Account``) that give the positions their
    class; ``Total <group>`` lines are subtotals, ``Total Holdings`` (or
    another :data:`GRAND_TOTALS` label, else the first unlabelled ``Total``)
    is the printed holdings total. A security's symbol is the ticker in
    parentheses ending its name (``APPLE INC (AAPL)``); a name wrapped onto
    the next lines is joined until its ``(SYMBOL)`` (also a line worded like
    a section heading: ``INCOME FUND (ZZSIX)``), and ``CUSIP: ...`` /
    ``ISIN: ...`` lines under a row give its identifiers; note lines
    (``— 7-day yield: 0.01%``) are read for nothing. Only a holding
    nothing else identifies takes a ticker printed in front of its name
    (:func:`_leading_ticker`: ``ZZAB Example Index Fund``; never in an
    all-capitals name, whose first word is an issuer's); one with none at
    all is kept, **named by its name alone** (``SecurityRef`` with only a
    name: an unlisted partnership, a 529 portfolio), and a sentence under
    its name in capitals (a word a title would capitalise in lower case:
    ``Valued from an independent estimate``) is a note on it, not its name
    (:func:`_sentence_under_capitals`); a line in title case (``Class R6``,
    ``2031 (Example Funds)``) is the rest of the name, and a holding with a
    symbol, or a ``CUSIP:`` line after the sentence, keeps the sentence in
    its name. Footnote markers
    in figures (``147.85A``, ``$8,412.36 c``) are dropped,
    ``unknown``/``N/A`` cost basis is unknown. A price is read per
    100 of face value (a bond: :attr:`InvestmentPosition.price_per_100`)
    when quantity x price / 100 is the printed market value and quantity x
    price is not. A cash sweep printed as a holding, whose value is the
    closing cash printed, is the account's cash when the positions add up
    to the account value only without it (:meth:`_sweep_as_cash`);
  - an **activity** table: a header naming a date, an activity/action and
    an amount (optional settle date, description, security name,
    symbol or CUSIP, quantity, price, fees; :data:`ACTIVITY_HEADERS`:
    *Transaction Description*, *Transaction Total*, *Settlement Date*,
    *Symbol/CUSIP*, *Transaction Cost*, ...). Only a settlement or posting
    date: it dates the row; no action column: the description is the
    action when the table also names a security, a symbol or a quantity
    (always for a layout with ``description_types``), and in an account
    with a holdings or security table also for a cash table (date,
    description, amount: deposits, card activity, fees). A header whose
    cells repeat one group is that many **tables side by side**, each read
    in its own half of the lines (``Date | Description | Amount`` twice).
    The **section** heading above a table (:data:`ACTIVITY_SECTIONS`) says
    how its rows are read, until the next heading (after the section's own
    table, a heading no section names right above a table ends it; right
    under the section's heading it is a sub-heading, a card's name; the
    section's header repeated on a later page continues it at the page's
    top or under the page's running header, every line above it on the
    page, each printed in the header band of that page and of the page the
    section's header was on (and heading no table of its own there),
    naming the account or the page, a date or period line or saying the
    table runs on (``continued``); a line saying nothing of these too (a
    figure as well), when another line of the block names the account or
    the page (a name under ``Page 2 of 4``; known limit: so is a heading
    without a list word there):
    :meth:`~BrokerageStatementParser._continued`; a line holding a list
    word (:data:`_LIST_WORDS`: ``Other Transactions``, ``Miscellaneous
    Items, page 2``) is a heading there unless it is the statement's title
    (:func:`_titled`: ``Monthly Activity Statement``, ``Account Activity
    for XXXX-1234``; only all-entries words, ``statement`` or ``report``,
    title filler and the account or the page, so not ``Miscellaneous Items
    Report``), as is any other line, and ends the section as on one
    page):
    trades pending settlement, cash-flow summaries and daily balances are
    no activity; margin interest is a fee dated by the last day of its
    period (``7/1-7/31``); a second card's table under a heading worded
    like the first card's stays in the card section; securities
    transferred in/out move
    units (the amount printed is their value) or, without units, cash;
    deposits, withdrawals, checking, debit card and bill payments are cash
    by their sign; income is a dividend or interest (a ``Return of
    capital`` there too, the words kept in its memo); fees and taxes
    withheld are fees. The action text types a row first
    (:func:`activity_type`, :data:`ACTIVITY_TYPES`), the section the rest
    (text naming no action is then the row's description; a description
    that is a held security's name names that security, one held by its
    name alone too; a trade or transfer of units with no symbol or CUSIP
    is named by its *Security Name* column alone, never by a description);
    an unsigned amount takes the type's direction (a buy takes cash out)
    unless its table prints minus signs (then it is an inflow); a transfer
    of units printed without an amount (``-``) moves no cash; a split's
    ratio comes from its description (``2-for-1``, ``3:1``); a trade's
    price is per 100 of face value
    (:attr:`InvestmentActivity.price_per_100`, :func:`_activity_per_100`)
    by its figures or, when they cannot tell, as its position is priced. A
    header line of known column names that is no usable table ends the
    table above it. A row no words type is not read: it is counted, and
    its action phrase and the heading above it are reported
    (``ParseOutcome.unknown_actions`` / ``unknown_headings``). The optional
    local model may suggest what kind of activity they are
    (:mod:`app.ingestion.parsers.vocabulary`, ADR-0010); read again with
    that :class:`~app.ingestion.parsers.vocabulary.Vocabulary`
    (:meth:`BrokerageStatementParser.parse_with`), such a row is typed, its
    figures still the page's own, and marked ``suggested_type`` so that a
    person confirms it;
  - **blocks side by side** (a summary box left of a table, two summary
    tables, a table beside a chart's legend) are read one after the other
    (:mod:`app.ingestion.parsers.blocks`), so a table header sharing its
    line with the other block's figure is still a header and a label keeps
    its amounts;
  - the statement's own **summary tables**, line by line
    (``InvestmentStatement.figures``,
    :class:`~app.ingestion.sandbox.protocol.StatementFigure`): the
    account-value bridge from ``Beginning Account Value`` to ``Ending
    Account Value``, the core account's cash flow, the income summary and
    the realized gains (:data:`FIGURE_SECTIONS`). They are never activity;
    the cash-flow lines are a second account of every section
    (:func:`localise`);
  - outside the tables: the period, the account's last four digits, the
    currency, opening/closing cash (also ``Beginning/Ending Core Account
    Balance``, a cash balance line with a column for this period and the
    last, and dated cash balance lines: :func:`_dated_cash`), the holdings
    total and the account value by label
    (``Beginning Account Value`` is consumed, not read as the account
    value; ``Ending Account Value``/``Ending Net Value`` are), and the
    holdings' "as of" date.

  Each value is scored like the bank parsers
  (:mod:`app.ingestion.parsers.confidence`): the declared format of an
  institution parser 1.0, the generic parser :data:`~confidence.GENERIC`,
  OCR scaled by the cell's word confidence, a position whose market value
  disagrees :data:`~confidence.CONTRADICTED`, and the rows a statement that
  does not reconcile leaves in doubt (:func:`localise`) capped at
  :data:`~confidence.UNRECONCILED` (every row at
  :data:`~confidence.UNCHECKED` when nothing can be checked).

Institution parsers pin a layout: :class:`PlaceholderBrokerageParser` the
fictional Placeholder Brokerage (fingerprint ``placeholder-brokerage-account``;
synthetic PDFs from ``tests/fixtures/synth_brokerage.py``) and
:class:`FidelityStatementParser` the Fidelity Investments "Investment
Report" of one or several accounts (fingerprint
``fidelity-investments-statement``; synthetic PDFs from
``tests/fixtures/synth_fidelity.py`` and ``synth_fidelity_report.py``),
detected by its name on page 1, in the PDF's title or author, or by its
brokerage subsidiary's name anywhere beside the layout's own wording
(:meth:`BrokerageStatementParser.named`), whose core money market position is
the account's cash (``cash_groups``) and whose *Core Fund Activity* table
is not read (``ignored_tables``). Every parser here keeps only the last four
digits of an account number (an account heading is read for them and not
kept).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any, ClassVar, Final, Literal, cast

from pydantic import ValidationError

from app.core.money import sum_amounts
from app.ingestion.parsers import confidence as conf
from app.ingestion.parsers.accounts import (
    HEADING_TOP_LINES,
    AccountInfo,
    AccountSegment,
    account_heading,
    number_last4,
    segment,
    summary_last4,
)
from app.ingestion.parsers.base import (
    Cell,
    DocumentParser,
    DocumentView,
    ParseOutcome,
    ViewLine,
    join_cells,
)
from app.ingestion.parsers.blocks import unfold_blocks
from app.ingestion.parsers.table_statement import (
    PeriodCandidates,
    PeriodRead,
    TableStatementParser,
    follow_order,
    full_style,
    label_end,
    read_period,
)
from app.ingestion.parsers.text import (
    FULL_STYLES,
    MAX_TOKEN,
    NUMERIC_DM,
    Amount,
    DateStyle,
    after_label,
    find_amounts,
    find_dates,
    guess_numeric_style,
    normalise,
    numeric_parts,
    read_amount,
    read_date,
)
from app.ingestion.parsers.vocabulary import (
    ACTION_PREFIX,
    COLUMN_PREFIX,
    MAX_PHRASES,
    SECTION_PREFIX,
    SUGGESTED_COLUMNS,
    SUGGESTED_DOUBT,
    SUGGESTED_SECTIONS,
    SUGGESTED_TYPES,
    Vocabulary,
    column_key,
    phrase_key,
)
from app.ingestion.reconcile import TOLERANCE
from app.ingestion.sandbox.protocol import (
    MAX_FIGURES,
    MAX_SECTION_CHECKS,
    MAX_STATEMENTS,
    SECTION_LABEL_MAX,
    SECURITY_NAME_MAX,
    AssetClassCode,
    Balance,
    FigureGroup,
    InvestmentActivity,
    InvestmentActivityType,
    InvestmentPosition,
    InvestmentReconciliation,
    InvestmentStatement,
    ParserRef,
    SectionCheck,
    SecurityRef,
    Statement,
    StatementAccount,
    StatementFigure,
    has_word,
)

# ---------------------------------------------------------------------------
# Reconciliation (any source)
# ---------------------------------------------------------------------------


def _price_unit(price: Decimal) -> Decimal:
    """Half a unit of the last decimal the price was given with."""
    exponent = price.as_tuple().exponent
    places = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    return Decimal(5).scaleb(-(places + 1))


def position_value(position: InvestmentPosition) -> Decimal | None:
    """The position's market value: printed, else quantity x price (per 100 for a bond)."""
    if position.market_value is not None:
        return position.market_value
    price = position.price_per_unit
    if price is not None:
        return position.quantity * price
    return None


def _position_agrees(quantity: Decimal, price: Decimal, value: Decimal, scale: int) -> bool:
    """quantity x price / ``scale`` = value, within the price's rounding (module doc)."""
    expected = quantity * price / scale
    allowed = TOLERANCE + abs(quantity) * _price_unit(price) / scale
    return abs(expected - value) <= allowed


def reconcile_investment(
    statement: InvestmentStatement, index_value: Decimal | None = None
) -> InvestmentReconciliation:
    """The brokerage checks (module doc).

    ``index_value``: the account's ending value as the document's account
    index prints it (a document of several accounts); compared with the
    account value printed on the account's own pages.
    """
    checks: list[str] = []
    failed: list[str] = []
    mismatches: list[int] = []
    for index, position in enumerate(statement.positions):
        if position.unit_price is None or position.market_value is None:
            continue
        scale = 100 if position.price_per_100 else 1
        if not _position_agrees(
            position.quantity, position.unit_price, position.market_value, scale
        ):
            mismatches.append(index)
    if any(p.unit_price is not None and p.market_value is not None for p in statement.positions):
        checks.append("positions")
        if mismatches:
            failed.append("positions")

    values = [position_value(p) for p in statement.positions]
    positions_total = (
        sum_amounts(v for v in values if v is not None)
        if all(v is not None for v in values)
        else None
    )
    reported_positions = statement.positions_value.amount if statement.positions_value else None
    if reported_positions is not None and positions_total is not None:
        checks.append("holdings_total")
        if abs(positions_total - reported_positions) > TOLERANCE:
            failed.append("holdings_total")

    opening = statement.cash_opening.amount if statement.cash_opening else None
    closing = statement.cash_closing.amount if statement.cash_closing else None
    reported_total = statement.total_value.amount if statement.total_value else None
    holdings = positions_total if positions_total is not None else reported_positions
    derived = False
    if (
        closing is None
        and opening is not None
        and reported_total is not None
        and holdings is not None
    ):
        # No closing cash printed: the account value less the holdings is the cash.
        closing, derived = reported_total - holdings, True
    activity = sum_amounts(a.amount for a in statement.activity)
    cash_difference: Decimal | None = None
    if opening is not None and closing is not None:
        checks.append("cash")
        cash_difference = opening + activity - closing
        if abs(cash_difference) > TOLERANCE:
            failed.append("cash")

    total_value = holdings + closing if holdings is not None and closing is not None else None
    if reported_total is not None and total_value is not None and not derived:
        checks.append("total_value")
        if abs(total_value - reported_total) > TOLERANCE:
            failed.append("total_value")

    if index_value is not None and reported_total is not None:
        checks.append("index_value")
        if abs(index_value - reported_total) > TOLERANCE:
            failed.append("index_value")

    status: Literal["ok", "mismatch", "unavailable"] = (
        "mismatch" if failed else ("ok" if checks else "unavailable")
    )
    return InvestmentReconciliation(
        status=status,
        checks=checks,
        failed=failed,
        position_mismatches=mismatches,
        positions_total=positions_total,
        reported_positions_total=reported_positions,
        cash_opening=opening,
        cash_closing=closing,
        cash_activity=activity,
        cash_difference=cash_difference,
        total_value=total_value,
        reported_total_value=reported_total,
        index_value=index_value,
        tolerance=TOLERANCE,
    )


# ---------------------------------------------------------------------------
# Reading numbers, symbols, activity words
# ---------------------------------------------------------------------------

_NUMBER: Final = re.compile(
    r"(?P<open>\()?(?P<sign>[-+\u2212])?(?P<cur>[$\u00a3\u20ac])?"
    r"(?P<int>\d{1,3}(?:,\d{3})+|\d{1,18})(?:\.(?P<dec>\d{1,10}))?(?P<close>\))?"
)
_SYMBOL: Final = re.compile(r"[A-Z][A-Z0-9.]{0,9}")
_PAREN_SYMBOL: Final = re.compile(r"\(([A-Z][A-Z0-9.]{0,9})\)")
_CUSIP: Final = re.compile(r"[0-9A-Z]{8}[0-9]")
_RATIO: Final = re.compile(r"\b(\d{1,6})(?:\s?-?\s?for\s?-?\s?|\s?:\s?)(\d{1,6})\b", re.IGNORECASE)
_CURRENCY: Final = re.compile(r"\bcurrency\s*:?\s*([A-Z]{3})\b", re.IGNORECASE)
#: A date at the end of a (normalised) header cell: ``ending market value oct 31 2026``.
_TRAILING_DATE: Final = re.compile(
    r"(?:\sas\sof)?\s(?:[a-z]{3,9}\s\d{1,2}\s\d{4}|\d{1,2}\s\d{1,2}\s\d{2,4}|\d{4}\s\d{1,2}\s\d{1,2})$"
)


def read_number(text: str) -> Decimal | None:
    """A quantity or unit price (any number of decimals, ``$``, thousands commas)."""
    match = _NUMBER.fullmatch(text.strip()[:MAX_TOKEN])
    if match is None or bool(match["open"]) != bool(match["close"]):
        return None
    digits = match["int"].replace(",", "")
    if match["dec"]:
        digits = f"{digits}.{match['dec']}"
    try:
        value = Decimal(digits)
    except InvalidOperation:  # pragma: no cover - the pattern only admits digits
        return None
    negative = bool(match["open"]) or match["sign"] in ("-", "\u2212")
    return -value if negative else value


def is_cusip(text: str) -> bool:
    """A CUSIP: 8 characters with at least one digit, then a valid check digit."""
    if _CUSIP.fullmatch(text) is None or not any(c.isdigit() for c in text[:8]):
        return False
    total = 0
    for index, char in enumerate(text[:8]):
        value = int(char) if char.isdigit() else ord(char) - ord("A") + 10
        if index % 2:
            value *= 2
        total += value // 10 + value % 10
    return (10 - total % 10) % 10 == int(text[8])


#: Normalised activity word -> type (the first word(s) of the activity cell).
ACTIVITY_TYPES: Final[Mapping[str, InvestmentActivityType]] = {
    "buy": "buy",
    "bought": "buy",
    "you bought": "buy",
    "purchase": "buy",
    "sell": "sell",
    "sold": "sell",
    "you sold": "sell",
    "sale": "sell",
    "dividend": "dividend",
    "cash dividend": "dividend",
    "dividend received": "dividend",
    "qualified dividend": "dividend",
    "capital gain": "dividend",
    "long term cap gain": "dividend",
    "short term cap gain": "dividend",
    "long term capital gain": "dividend",
    "short term capital gain": "dividend",
    "interest": "interest",
    "interest earned": "interest",
    "interest received": "interest",
    "muni exempt interest": "interest",
    "tax exempt interest": "interest",
    "bond interest": "interest",
    "fee": "fee",
    "fee charged": "fee",
    "account fee": "fee",
    "commission": "fee",
    "foreign tax paid": "fee",
    "reinvest": "reinvest",
    "reinvestment": "reinvest",
    "dividend reinvestment": "reinvest",
    "split": "split",
    "stock split": "split",
    "transfer in": "transfer_in",
    "shares in": "transfer_in",
    "transfer out": "transfer_out",
    "shares out": "transfer_out",
    "deposit": "deposit",
    "direct deposit": "deposit",
    "electronic funds transfer received": "deposit",
    "contribution": "deposit",
    "withdrawal": "withdrawal",
    "electronic funds transfer paid": "withdrawal",
    "distribution": "withdrawal",
    "normal distribution": "withdrawal",
    "corporate action": "corporate_action",
    "merger": "corporate_action",
    "spinoff": "corporate_action",
    # Fidelity's cash-management and retirement wording (activity exports).
    "redemption": "sell",
    "redeemed": "sell",
    "redemption from core account": "sell",
    "purchase into core account": "buy",
    "check paid": "withdrawal",
    "check received": "deposit",
    "debit card purchase": "withdrawal",
    "bill payment": "withdrawal",
    "bill pay": "withdrawal",
    "direct debit": "withdrawal",
    "cash in lieu": "deposit",
    "return of capital": "deposit",
    "partic contr": "deposit",
    "participant contribution": "deposit",
    "employer contr": "deposit",
    "employer contribution": "deposit",
    "credit interest": "interest",
    "margin interest": "fee",
    "margin interest paid": "fee",
    "advisory fee": "fee",
    "advisor fee": "fee",
    "management fee": "fee",
    "adr fee": "fee",
    "tax withheld": "fee",
    "fed tax withheld": "fee",
    "federal tax withheld": "fee",
    "state tax withheld": "fee",
    "federal tax paid": "fee",
    "state tax paid": "fee",
    "withholding tax": "fee",
    "nonresident tax": "fee",
    "non resident tax": "fee",
    "foreign tax": "fee",
}
#: Phrases naming a transfer whose type depends on what moved: ``(type when a
#: security and a quantity are present, type for cash)``.
TRANSFER_TYPES: Final[Mapping[str, tuple[InvestmentActivityType, InvestmentActivityType]]] = {
    "transferred from": ("transfer_in", "deposit"),
    "transferred to": ("transfer_out", "withdrawal"),
    "trans from": ("transfer_in", "deposit"),
    "trans to": ("transfer_out", "withdrawal"),
    "transfer from": ("transfer_in", "deposit"),
    "transfer to": ("transfer_out", "withdrawal"),
}
#: Phrases whose direction only the row's signs tell: ``(units in, units out)``
#: when the row moves a security (by the quantity's sign), else cash in or
#: out by the amount's sign (``deposit``/``withdrawal``). A journal entry,
#: a rollover or a conversion moves units or cash either way; an exchange,
#: an option assignment or expiry is a buy or a sale of units.
SIGNED_TYPES: Final[Mapping[str, tuple[InvestmentActivityType, InvestmentActivityType]]] = {
    "journaled": ("transfer_in", "transfer_out"),
    "journal": ("transfer_in", "transfer_out"),
    "transfer": ("transfer_in", "transfer_out"),
    "transfer of assets": ("transfer_in", "transfer_out"),
    "wire transfer": ("transfer_in", "transfer_out"),
    "electronic funds transfer": ("transfer_in", "transfer_out"),
    "rollover": ("transfer_in", "transfer_out"),
    "roth conversion": ("transfer_in", "transfer_out"),
    "conversion": ("transfer_in", "transfer_out"),
    "exchanged": ("buy", "sell"),
    "exchange": ("buy", "sell"),
    "you exchanged": ("buy", "sell"),
    "assigned": ("buy", "sell"),
    "expired": ("buy", "sell"),
}
#: Account types an investment statement may take from the document (``account_kind``).
INVESTMENT_KINDS: Final = frozenset({"brokerage", "retirement"})
#: Types whose cash effect is an outflow when the amount is printed unsigned.
OUTFLOW_TYPES: Final = frozenset({"buy", "fee", "withdrawal", "reinvest"})
#: Types that must name a security / carry a quantity (``investment_txn`` checks).
SECURITY_TYPES: Final = frozenset(
    {"buy", "sell", "split", "transfer_in", "transfer_out", "reinvest"}
)
QUANTITY_TYPES: Final = frozenset({"buy", "sell", "transfer_in", "transfer_out", "reinvest"})
#: Cash rows whose action text is their memo when nothing else describes them.
CASH_MEMO_TYPES: Final = frozenset({"deposit", "withdrawal", "fee", "interest"})


def split_ratio(text: str) -> tuple[Decimal, Decimal] | None:
    """``(new, old)`` from ``2 for 1`` / ``2-for-1`` / ``2:1`` in a split's description."""
    match = _RATIO.search(text[:MAX_TOKEN])
    if match is None:
        return None
    new, old = Decimal(match[1]), Decimal(match[2])
    if new <= 0 or old <= 0 or new == old:
        return None
    return new, old


def activity_type(
    text: str,
    *,
    moves_security: bool = False,
    units_sign: int = 0,
    cash_sign: int = 0,
) -> InvestmentActivityType | None:
    """The type an activity text names (the longest known phrase at its start).

    Case and punctuation do not matter and anything after the phrase is
    ignored, so a broker's action text with the security and a trailing
    ``(Cash)``/``(Margin)`` is typed by its leading words
    (``REINVESTMENT EXAMPLE FUND (EXMPL) (Cash)`` -> ``reinvest``). It reads
    a PDF's activity cell or a CSV's action column alike.

    ``Transferred from``/``Transferred to`` (:data:`TRANSFER_TYPES`) is a
    ``transfer_in``/``transfer_out`` of units when ``moves_security`` (the
    row names a security and a non-zero quantity), else cash: ``deposit``/
    ``withdrawal``.

    A phrase of :data:`SIGNED_TYPES` (``JOURNALED``, ``ROLLOVER``,
    ``EXCHANGED``, ...) takes its direction from the row: with
    ``moves_security`` the sign of the quantity (``units_sign``, negative =
    units leave), else the sign of the amount (``cash_sign``, negative =
    cash leaves: ``withdrawal``, else ``deposit``). Without a sign it is
    None: the direction is never guessed.
    """
    norm = normalise(text)
    best: InvestmentActivityType | None = None
    best_len = 0

    def starts(phrase: str) -> bool:
        return norm == phrase or norm.startswith(f"{phrase} ")

    for phrase, kind in ACTIVITY_TYPES.items():
        if len(phrase) > best_len and starts(phrase):
            best, best_len = kind, len(phrase)
    for phrase, (units, cash) in TRANSFER_TYPES.items():
        if len(phrase) > best_len and starts(phrase):
            best, best_len = (units if moves_security else cash), len(phrase)
    for phrase, (inward, outward) in SIGNED_TYPES.items():
        if len(phrase) > best_len and starts(phrase):
            sign = units_sign if moves_security else cash_sign
            if moves_security:
                best = outward if sign < 0 else inward
            else:
                best = "withdrawal" if sign < 0 else "deposit"
            if sign == 0:
                best = None
            best_len = len(phrase)
    return best


def guess_asset_class(name: str | None) -> AssetClassCode | None:
    """A rough class from the security's name (an institution's own words)."""
    words = set(normalise(name or "").split())
    if {"etf", "exchange", "traded"} & words:
        return "etf"
    if {"money", "market", "cash", "sweep"} & words:
        return "cash_equivalent"
    if {"fund", "funds", "portfolio", "trust"} & words:
        return "mutual_fund"  # a bond fund is a fund
    if {"bond", "bonds", "treasury", "note", "notes", "municipal"} & words:
        return "bond"
    return None


# ---------------------------------------------------------------------------
# Table vocabulary
# ---------------------------------------------------------------------------

#: ``begin`` is a beginning-of-period market value (read for cash groups only);
#: ``other`` a column that is recognised (so its cells are not mistaken for
#: another column's) but not read.
HoldingRole = Literal["symbol", "name", "quantity", "price", "value", "cost", "begin", "other"]
#: ``name`` is a security-name column next to a description column.
ActivityRole = Literal[
    "date",
    "settle",
    "type",
    "description",
    "name",
    "symbol",
    "quantity",
    "price",
    "amount",
    "fees",
    "other",
]

HOLDING_HEADERS: Final[Mapping[str, HoldingRole]] = {
    "symbol": "symbol",
    "ticker": "symbol",
    "symbol cusip": "symbol",
    "cusip": "symbol",
    "description": "name",
    "security": "name",
    "security description": "name",
    "security name": "name",
    "investment": "name",
    "name": "name",
    "quantity": "quantity",
    "shares": "quantity",
    "units": "quantity",
    "qty": "quantity",
    "price": "price",
    "unit price": "price",
    "closing price": "price",
    "price per share": "price",
    "price per unit": "price",
    "market price": "price",
    "market value": "value",
    "current value": "value",
    "ending market value": "value",
    "ending value": "value",
    "value": "value",
    "cost basis": "cost",
    "total cost basis": "cost",
    "total cost": "cost",
    "cost": "cost",
    "book cost": "cost",
    "beginning market value": "begin",
    "beginning value": "begin",
    "unrealized gain loss": "other",
    "unrealized gain": "other",
    "gain loss": "other",
    "eai ey": "other",
    "eai": "other",
    "estimated annual income": "other",
    "est annual income": "other",
    "est annual income eai": "other",
    "income eai": "other",
    "est yield": "other",
    "est yield ey": "other",
    "estimated yield": "other",
    "ey": "other",
    "accrued interest": "other",
    "accrued interest ai": "other",
    "maturity": "other",
    "maturity date": "other",
    "coupon": "other",
    "coupon rate": "other",
    "rate": "other",
    "percent of account": "other",
    "percent of total value": "other",
    "percent of portfolio": "other",
}
ACTIVITY_HEADERS: Final[Mapping[str, ActivityRole]] = {
    "date": "date",
    "trade date": "date",
    "transaction date": "date",
    "trans date": "date",
    "settle date": "settle",
    "settlement date": "settle",
    "post date": "settle",
    "posting date": "settle",
    "date paid": "settle",
    "activity": "type",
    "action": "type",
    "transaction": "type",
    "type": "type",
    "transaction type": "type",
    "activity type": "type",
    "description": "description",
    "transaction description": "description",
    "activity description": "description",
    "location description": "description",
    "location reference description": "description",
    "payee": "description",
    "security": "description",
    "details": "description",
    "security name": "name",
    "security description": "name",
    "investment": "name",
    "symbol": "symbol",
    "symbol cusip": "symbol",
    "cusip": "symbol",
    "ticker": "symbol",
    "quantity": "quantity",
    "shares": "quantity",
    "units": "quantity",
    "price": "price",
    "unit price": "price",
    "price per share": "price",
    "price per unit": "price",
    "amount": "amount",
    "net amount": "amount",
    "transaction total": "amount",
    "transaction amount": "amount",
    "total amount": "amount",
    "net cash": "amount",
    "fees": "fees",
    "commission": "fees",
    "commissions": "fees",
    "commission fees": "fees",
    "transaction cost": "fees",
    "transaction costs": "fees",
    "reference": "other",
    "reference description": "other",
    "account type": "other",
    "balance": "other",
    "cost basis": "other",
    "cost basis of close": "other",
    "realized gain loss": "other",
    "check num": "other",
    "check no": "other",
    "check number": "other",
    "code": "other",
    "payee account": "other",
    "ytd payments": "other",
    "accrued interest": "other",
    # A margin interest table: one row per period, the interest it charged.
    "period": "date",
    "interest paid": "amount",
    "period balance": "other",
    "interest rate": "other",
    "average daily balance": "other",
}
#: Asset-class group headings of a holdings table (normalised) -> the class
#: of the rows under them. ``Total <group>`` lines are subtotals.
ASSET_GROUPS: Final[Mapping[str, AssetClassCode | None]] = {
    "mutual funds": "mutual_fund",
    "exchange traded products": "etf",
    "exchange traded funds": "etf",
    "etfs": "etf",
    "stocks": "equity",
    "equities": "equity",
    "common stock": "equity",
    "bonds": "bond",
    "fixed income": "bond",
    "options": "option",
    "money market funds": "cash_equivalent",
    "core account": "cash_equivalent",
    "cash and cash equivalents": "cash_equivalent",
    # Sub-groups under the groups above.
    "bond funds": "mutual_fund",
    "stock funds": "mutual_fund",
    "short term funds": "mutual_fund",
    "common stocks": "equity",
    "preferred stock": "equity",
    "preferred stocks": "equity",
    "corporate bonds": "bond",
    "municipal bonds": "bond",
    "government bonds": "bond",
    "asset backed securities": "bond",
    "us treasury agency securities": "bond",
    "other bonds": "bond",
    "other": None,
}
#: ``Total <these>`` in a holdings table is the grand total (beats an unlabelled ``Total``).
GRAND_TOTALS: Final = ("holdings", "market value", "securities", "investments", "portfolio")


#: How an activity section's rows are read (:data:`ACTIVITY_SECTIONS`):
#:
#: * ``plain``: by their action words only (also ends the section before);
#: * ``skip``: not activity (pending trades, cash-flow summaries, daily
#:   balances, estimates): its table rows are not read;
#: * ``transfer_in``/``transfer_out``: a row moving units is a transfer of
#:   units (its printed amount is the value moved, no cash); a row without
#:   units moves cash (``deposit``/``withdrawal``);
#: * ``income``: a dividend, or interest when its words say so;
#: * ``deposit``/``withdrawal``/``card``/``cash``: cash by the amount's sign
#:   (an unsigned amount of a table printing no signs: a deposit, a
#:   withdrawal, a withdrawal, a deposit);
#: * ``fee``: a fee or tax withheld;
#: * ``margin``: margin interest, one row per period (``7/1-7/31``) dated
#:   by the period's last day: a fee.
#:
#: Action words the row prints come first; the section types the rest.
SectionKind = Literal[
    "plain",
    "skip",
    "transfer_in",
    "transfer_out",
    "income",
    "deposit",
    "withdrawal",
    "card",
    "cash",
    "fee",
    "margin",
]
#: Activity section headings (normalised prefixes, the longest wins).
ACTIVITY_SECTIONS: Final[Mapping[str, SectionKind]] = {
    "securities bought sold": "plain",
    "securities bought and sold": "plain",
    "securities purchased and sold": "plain",
    "trade activity": "plain",
    "trades": "plain",
    "buys and sells": "plain",
    "purchases and sales": "plain",
    "other activity": "plain",
    "corporate actions": "plain",
    "holdings": "plain",
    "portfolio holdings": "plain",
    "positions": "plain",
    "account summary": "plain",
    "trades pending settlement": "skip",
    "pending trades": "skip",
    "pending settlement": "skip",
    "pending transactions": "skip",
    "unsettled trades": "skip",
    "core account cash flow": "skip",
    "cash flow summary": "skip",
    "estimated cash flow": "skip",
    "daily additions and subtractions": "skip",
    "daily balance": "skip",
    "daily ending balance": "skip",
    "income summary": "skip",
    "realized gains": "skip",
    "margin activity": "margin",
    "margin interest": "margin",
    "top holdings": "skip",
    "account holdings": "skip",
    "debit card summary": "skip",
    "securities transferred in": "transfer_in",
    "securities received": "transfer_in",
    "transfers in": "transfer_in",
    "securities transferred out": "transfer_out",
    "securities delivered": "transfer_out",
    "transfers out": "transfer_out",
    "dividends interest": "income",
    "dividends and interest": "income",
    "dividends": "income",
    "interest income": "income",
    "income": "income",
    "deposits": "deposit",
    "contributions": "deposit",
    "withdrawals": "withdrawal",
    "distributions": "withdrawal",
    "checking activity": "card",
    "checks paid": "card",
    "checks written": "card",
    "debit card": "card",
    "card activity": "card",
    "bill payment": "card",
    "bill payments": "card",
    "cash activity": "cash",
    "electronic funds transfers": "cash",
    "electronic funds transfer": "cash",
    "cash transactions": "cash",
    "cash management activity": "cash",
    "fees and charges": "fee",
    "fees charges": "fee",
    "fees": "fee",
    "taxes withheld": "fee",
    "tax withheld": "fee",
    "taxes": "fee",
}
#: Section kinds whose rows take their type from the amount's sign.
_CASH_SECTIONS: Final[Mapping[SectionKind, InvestmentActivityType]] = {
    "deposit": "deposit",
    "withdrawal": "withdrawal",
    "card": "withdrawal",
    "cash": "deposit",
}


def section_rule(norm: str) -> SectionKind | None:
    """The kind of activity section a (normalised) heading names, if any."""
    best: SectionKind | None = None
    best_len = 0
    for phrase, kind in ACTIVITY_SECTIONS.items():
        if len(phrase) > best_len and (norm == phrase or norm.startswith(f"{phrase} ")):
            best, best_len = kind, len(phrase)
    return best


#: Headings of a statement's own summary tables (normalised prefixes) -> the
#: figures under them (:class:`~app.ingestion.sandbox.protocol.StatementFigure`).
FIGURE_SECTIONS: Final[Mapping[str, FigureGroup]] = {
    "core account cash flow": "cash_flow",
    "cash flow summary": "cash_flow",
    "cash flow": "cash_flow",
    "income summary": "income",
    "realized gains": "realized",
    "realized gain": "realized",
}
#: Labels opening and closing the account-value bridge, wherever it is printed.
VALUE_OPENING: Final = (
    "beginning account value",
    "beginning net value",
    "beginning portfolio value",
)
VALUE_CLOSING: Final = ("ending account value", "ending net value", "ending portfolio value")
#: The label that ends a group of figures (a prefix; ``cash_flow`` and
#: ``account_value`` end at their closing figure).
_FIGURE_END: Final[Mapping[FigureGroup, str]] = {"income": "total", "realized": "net gain loss"}
#: First words of a line that sums the rows above it.
_TOTAL_WORDS: Final = frozenset({"total", "subtotal", "net"})
#: A total's words that say it covers every row of its section.
_WHOLE_SECTION: Final = frozenset({"", "this period", "activity", "transactions"})
_BUY_WORDS: Final = frozenset({"bought", "purchased", "purchases", "buys"})
_SELL_WORDS: Final = frozenset({"sold", "sales", "sells"})
_TRANSFER_WORDS: Final = frozenset({"transfers", "transfer", "transferred"})
#: Headings inside a cash-flow summary (its two halves), never a section's.
_FLOW_HEADINGS: Final = frozenset({"investment activity", "cash management activity"})
#: Cash-flow summary lines that restate what other lines already count.
_FLOW_RESTATED: Final = ("transaction costs",)


def figure_group(norm: str) -> FigureGroup | None:
    """The group of figures a (normalised) heading opens, if any."""
    best: FigureGroup | None = None
    best_len = 0
    for phrase, group in FIGURE_SECTIONS.items():
        if len(phrase) > best_len and (norm == phrase or norm.startswith(f"{phrase} ")):
            best, best_len = group, len(phrase)
    return best


@dataclass(frozen=True)
class _Column[R]:
    role: R
    x0: float
    x1: float


@dataclass(frozen=True)
class _HeaderCell:
    """A header cell, possibly over several lines (``Price`` / ``Per Unit``)."""

    parts: tuple[str, ...]
    x0: float
    x1: float

    @property
    def key(self) -> str:
        return normalise(" ".join(self.parts))


#: Beyond any page: the outer edges of the first and last side-by-side table.
_FAR: Final = 1e9
#: A line of more cells than this is not taken for an unknown table's header.
_MAX_UNKNOWN_HEADER: Final = 12


@dataclass(frozen=True)
class _Part:
    """One activity table of a header (several when tables sit side by side)."""

    columns: list[_Column[ActivityRole]]
    #: The span of the line this table reads (``-_FAR``/``_FAR``: open).
    lo: float
    hi: float
    #: ``column:<heading>`` when a column of this table is only the local
    #: model's suggestion (``app.ingestion.parsers.vocabulary``): its rows
    #: carry it and wait for a person.
    suggested: str | None = None


@dataclass(frozen=True)
class _Header:
    holdings: list[_Column[HoldingRole]] | None
    parts: list[_Part] | None
    #: Lines the header takes.
    used: int

    @property
    def roles(self) -> tuple[tuple[str, ...], ...]:
        """The column roles, per side-by-side table."""
        if self.holdings is not None:
            return (tuple(c.role for c in self.holdings),)
        return tuple(tuple(c.role for c in p.columns) for p in self.parts or ())

    @property
    def known_roles(self) -> tuple[tuple[str, ...], ...]:
        """:attr:`roles` without the columns no header word names (``other``)."""
        return tuple(tuple(r for r in part if r != "other") for part in self.roles)


#: Words a header cell is split into at most (bounded work).
_MAX_HEADER_WORDS: Final = 12


@lru_cache(maxsize=4096)
def _vocabulary(text: str) -> bool:
    """``text`` is a header phrase of either table (trailing date dropped)."""
    norm = normalise(text)
    return any(
        candidate in HOLDING_HEADERS or candidate in ACTIVITY_HEADERS
        for candidate in (norm, _TRAILING_DATE.sub("", norm))
    )


@lru_cache(maxsize=4096)
def split_header_cell(cell: Cell) -> tuple[Cell, ...]:
    """A header cell whose words are several known headers, split into them.

    Header words closer than the cell gap make one cell (``Transaction
    Description Quantity``, ``Check Num. Post Date Code Description``); when
    the cell is no header phrase itself but its words are a sequence of
    known phrases (the fewest), each phrase is its own cell with its words'
    boxes. Anything else is kept as it is.
    """
    if _vocabulary(cell.text):
        return (cell,)
    words = cell.word_cells()
    if not 2 <= len(words) <= _MAX_HEADER_WORDS:
        return (cell,)
    count = len(words)
    best: list[list[int] | None] = [None] * (count + 1)
    best[0] = []
    for end in range(1, count + 1):
        for start in range(end):
            before = best[start]
            if before is None or not _vocabulary(" ".join(w.text for w in words[start:end])):
                continue
            found = best[end]
            if found is None or len(before) + 1 < len(found):
                best[end] = [*before, end]
    cuts = best[count]
    if cuts is None or len(cuts) < 2:
        return (cell,)
    out: list[Cell] = []
    start = 0
    for end in cuts:
        out.append(join_cells(words[start:end]))
        start = end
    return tuple(out)


def _header_cells(lines: Sequence[ViewLine]) -> list[_HeaderCell]:
    """The cells of a header over one or more lines.

    The line with the most cells (after :func:`split_header_cell`; the
    first on a tie) gives the columns; a cell of the other lines over one
    column is added to it, in line order (``Price`` over ``Per Unit``), and
    one over several columns is shared out word by word, each word to the
    column it overlaps most (``Cost Basis Transaction`` over ``of Close`` and
    ``Cost``); the columns keep the spans of their own line (a title above the
    header widens none). Words over no column make cells of their own.
    """
    split = [[piece for c in line.cells for piece in split_header_cell(c)] for line in lines]
    base = max(range(len(lines)), key=lambda i: (len(split[i]), -i))
    spans = [[c.x0, c.x1] for c in split[base]]
    words: list[dict[int, list[str]]] = [{base: [c.text]} for c in split[base]]
    extra: list[_HeaderCell] = []
    for index, cells in enumerate(split):
        if index == base:
            continue
        for cell in cells:
            orphans: list[Cell] = []
            over = [n for n, (x0, x1) in enumerate(spans) if cell.overlap(x0, x1) > 0]
            if len(over) == 1:
                # Over one column: the whole cell is its part ("Total" / "Cost Basis").
                words[over[0]].setdefault(index, []).append(cell.text)
                continue
            for word in cell.word_cells():
                overlaps = [word.overlap(x0, x1) for x0, x1 in spans]
                best = max(range(len(spans)), key=lambda i: overlaps[i]) if spans else -1
                if best >= 0 and overlaps[best] > 0:
                    words[best].setdefault(index, []).append(word.text)
                else:
                    orphans.append(word)
            if orphans:
                joined = join_cells(orphans)
                extra.append(_HeaderCell((joined.text,), joined.x0, joined.x1))
    cells_out = [
        _HeaderCell(
            tuple(" ".join(by_line[i]) for i in sorted(by_line)),
            spans[n][0],
            spans[n][1],
        )
        for n, by_line in enumerate(words)
    ]
    return sorted([*cells_out, *extra], key=lambda h: h.x0)


def header_key[R](parts: Sequence[str], headers: Mapping[str, R]) -> R | None:
    """The role a header cell names: the most of its lines that are a known header.

    ``Ending Market Value`` over ``Oct 31, 2026`` and ``Price Per`` over
    ``Unit`` are read whole; a trailing date (``... as of 10/31/2026``) is
    dropped; a line below that is no part of the header (a group heading)
    is left off, and so is a line above it (a title over the header).
    """
    count = len(parts)
    for length in range(count, 0, -1):
        for start in range(count - length + 1):
            text = normalise(" ".join(parts[start : start + length]))
            for candidate in (text, _TRAILING_DATE.sub("", text)):
                if candidate in headers:
                    return headers[candidate]
    return None


def _columns[R](
    cells: Sequence[_HeaderCell], headers: Mapping[str, R], other: R
) -> tuple[list[_Column[R]], int]:
    """``(columns, cells naming a known header)``.

    A cell naming no header, or a role a cell to its left already took, is
    an ``other`` column: its values are recognised and not read, so they do
    not slide into a neighbouring column. Two description columns: the
    left one names the security (``Date | Security | Description |
    Amount``).
    """
    found: list[_Column[R]] = []
    roles: set[R] = set()
    known = 0
    for cell in sorted(cells, key=lambda h: h.x0):
        role = header_key(cell.parts, headers)
        if role is not None:
            known += 1
        if (
            role == "description"
            and "description" in roles
            and "name" in headers.values()
            and "name" not in roles
        ):
            first = next(i for i, c in enumerate(found) if c.role == "description")
            found[first] = _Column(cast(R, "name"), found[first].x0, found[first].x1)
            roles.add(cast(R, "name"))
        elif role is None or (role in roles and role != other):
            found.append(_Column(other, cell.x0, cell.x1))
            continue
        roles.add(role)
        found.append(_Column(role, cell.x0, cell.x1))
    return found, known


def _activity_headers(
    vocabulary: Vocabulary | None, *, suggestions: bool
) -> Mapping[str, ActivityRole]:
    """:data:`ACTIVITY_HEADERS` with the column headings the household learned
    and, with ``suggestions``, those the local model suggested (ADR-0010).

    The parser's own words always win; a learned heading wins over a suggested one.
    """
    if vocabulary is None:
        return ACTIVITY_HEADERS
    extra = {**(vocabulary.columns if suggestions else {}), **vocabulary.learned_columns}
    if not extra:
        return ACTIVITY_HEADERS
    return {
        **{k: cast(ActivityRole, v) for k, v in extra.items() if v in SUGGESTED_COLUMNS},
        **ACTIVITY_HEADERS,
    }


def _known_header(text: str) -> bool:
    parts = (text,)
    return header_key(parts, HOLDING_HEADERS) is not None or (
        header_key(parts, ACTIVITY_HEADERS) is not None
    )


def _header_like(line: ViewLine) -> bool:
    """Three or more cells, all header words, no amount: a table header we cannot use."""
    cells = [piece for c in line.cells for piece in split_header_cell(c)]
    return (
        len(cells) >= 3
        and not find_amounts(line.text)
        and all(_known_header(c.text) for c in cells)
    )


def _spread(line: ViewLine, columns: Sequence[_Column[Any]]) -> list[Cell]:
    """The line's cells, a cell over several columns split into its words.

    Values closer than the cell gap (``120 $35.61700``, ``96205LQK3 Muni
    exempt interest``) are one cell; each word goes to the column whose span
    holds its centre, a word in no column's span stays with the word before.
    """
    out: list[Cell] = []
    for cell in line.cells:
        if sum(1 for c in columns if cell.overlap(c.x0, c.x1) > 0) < 2:
            out.append(cell)
            continue
        groups: list[list[Cell]] = []
        current: _Column[Any] | None = None
        for word in cell.word_cells():
            home = next((c for c in columns if c.x0 <= word.centre <= c.x1), None)
            if groups and (home is None or home is current):
                groups[-1].append(word)
                continue
            groups.append([word])
            current = home
        out.extend(join_cells(group) for group in groups)
    return out


def _assign[R](line: ViewLine, columns: Sequence[_Column[R]]) -> dict[R, list[Cell]]:
    out: dict[R, list[Cell]] = {}
    for cell in _spread(line, columns):
        best: _Column[R] | None = None
        best_overlap = 0.0
        for column in columns:
            overlap = cell.overlap(column.x0, column.x1)
            if overlap > best_overlap:
                best, best_overlap = column, overlap
        if best is None and columns:
            nearest = min(columns, key=lambda c: abs((c.x0 + c.x1) / 2 - cell.centre))
            if abs((nearest.x0 + nearest.x1) / 2 - cell.centre) < max(3 * line.height, 30.0):
                best = nearest
        if best is not None:
            out.setdefault(best.role, []).append(cell)
    return out


def _text(cells: list[Cell] | None) -> str:
    return " ".join(c.text for c in cells or ()).strip()


def _ocr[R](cells: Mapping[R, list[Cell]]) -> float | None:
    values = [c.conf for group in cells.values() for c in group if c.conf is not None]
    return min(values) if values else None


def _starts(norm: str, phrase: str) -> bool:
    return norm == phrase or norm.startswith(f"{phrase} ")


# ---------------------------------------------------------------------------
# Collected rows
# ---------------------------------------------------------------------------


@dataclass
class _HoldingRow:
    page: int
    bbox: tuple[float, float, float, float]
    symbol: str | None
    name: str | None
    quantity: Decimal
    price: Decimal | None
    value: Amount | None
    cost: Amount | None
    ocr: float | None
    begin: Amount | None = None
    cusip: str | None = None
    #: The asset-class group heading the row is under (normalised).
    group: str | None = None
    #: The name cell as printed (continuation lines appended).
    raw_name: str | None = None
    #: The symbol came from a symbol column (not from the name).
    symbol_column: bool = False
    isin: str | None = None
    #: The name is complete (its ``(SYMBOL)`` or a ``CUSIP:`` line was read).
    closed: bool = False
    #: Sentence-case lines under a name in capitals that nothing identifies
    #: yet (:func:`_sentence_under_capitals`): a note on the holding unless an
    #: identifier or more of the name follows, which makes them part of it.
    notes: list[str] = field(default_factory=list)


@dataclass
class _ActivityRow:
    page: int
    bbox: tuple[float, float, float, float]
    date_text: str
    settle_text: str
    type: InvestmentActivityType
    description: str
    symbol: str | None
    quantity: Decimal | None
    price: Decimal | None
    amount: Amount
    fees: Amount | None
    ocr: float | None
    cusip: str | None = None
    #: The table the row is in (signs are read per table).
    table: int = 0
    #: The type is the section's: the amount's sign decides deposit or withdrawal.
    by_sign: bool = False
    #: The text of a security-name column (``Security Name``), when the table has one.
    security_name: str | None = None
    #: The heading the row is under, as printed, and its section's number.
    section_label: str | None = None
    section_id: int = 0
    #: The amount as printed (a transfer of units keeps its value here).
    printed: Decimal | None = None
    #: The table has a symbol column (so a name's first word is no ticker).
    symbol_column: bool = False
    #: Words of the row that say what it is beyond its type (``Return of capital``).
    note: str | None = None
    #: The row's type is the local model's suggestion (its words or its
    #: section's heading are in no vocabulary of the parser's own).
    suggested: bool = False
    #: The wording that suggestion is for, when it may be remembered
    #: (``InvestmentActivity.suggestion``).
    suggestion: str | None = None
    #: Lines printed under the row: how many wrapped its security's name, what
    #: the others say (``Wash sale of ...``, ``Accrued Interest: ...``), and
    #: where the row now ends on its page.
    name_lines: int = 0
    notes: list[str] = field(default_factory=list)
    bottom: float = 0.0


@dataclass(frozen=True)
class _SectionTotal:
    """A ``Total ...`` line of an activity table (:func:`_total_scope`)."""

    label: str
    norm: str
    amount: Decimal
    section_id: int
    table: int
    page: int


@dataclass
class _Summary:
    period: PeriodCandidates = field(default_factory=PeriodCandidates)
    as_of_text: str | None = None
    last4: str | None = None
    currency: str | None = None
    cash_opening: Amount | None = None
    cash_closing: Amount | None = None
    positions_value: Amount | None = None
    total_value: Amount | None = None
    #: The order of a "This Period | Last Period" column header: True when
    #: the current period is the first column.
    current_first: bool | None = None
    #: Dated cash balance lines (the text after the label, the amount).
    cash_dated: list[tuple[str, Amount]] = field(default_factory=list)


@dataclass
class _Collected:
    """What one account's lines yield (the whole document for one account)."""

    #: The account's lines (a document of several accounts).
    account: AccountSegment | None = None
    #: What the document says about the account (its index or heading).
    info: AccountInfo | None = None
    holdings: list[_HoldingRow] = field(default_factory=list)
    activity: list[_ActivityRow] = field(default_factory=list)
    #: Rows under a cash group heading (``cash_groups``): the account's cash.
    cash: list[_HoldingRow] = field(default_factory=list)
    summary: _Summary = field(default_factory=_Summary)
    skipped: int = 0
    unknown_activity: int = 0
    holdings_header: bool = False
    activity_header: bool = False
    # -- reading state ------------------------------------------------------------
    holdings_columns: list[_Column[HoldingRole]] | None = None
    activity_parts: list[_Part] | None = None
    #: The activity section the lines are in (:data:`ACTIVITY_SECTIONS`).
    section: SectionKind | None = None
    #: Tables read so far (each activity row knows its table).
    tables: int = 0
    #: Activity tables read since the last section heading.
    section_tables: int = 0
    #: The column roles and page of the section's latest table header: the
    #: same header repeated on a later page under the page's running header
    #: continues the section.
    section_header: tuple[tuple[tuple[str, ...], ...], int] | None = None
    #: Each page's header band (:func:`_page_bands`) of the whole document.
    bands: Mapping[int, frozenset[str]] = field(default_factory=dict)
    #: Lines (:func:`_band_key`) that headed a table of their own, by the
    #: pages they did it on: such a line is no running page header there.
    headings: dict[str, set[int]] = field(default_factory=dict)
    group: str | None = None
    last_holding: _HoldingRow | None = None
    #: Inside a table the layout says not to read (``ignored_tables``).
    ignoring: bool = False
    #: Where the holdings total came from: an unlabelled or a ``Total Holdings`` line.
    table_total: Literal["plain", "grand"] | None = None
    #: The heading of the section the lines are in (as printed) and its number.
    section_label: str | None = None
    section_id: int = 0
    #: Heading-like lines between the section's heading and its first table
    #: (a card's name): a later heading like one of them continues the section.
    sub_headings: list[str] = field(default_factory=list)
    #: ``Total ...`` lines of the activity tables.
    totals: list[_SectionTotal] = field(default_factory=list)
    #: The statement's own summary tables, line by line, and the group being read.
    figures: list[StatementFigure] = field(default_factory=list)
    figure_group: FigureGroup | None = None
    figure_x: float | None = None
    figure_page: int | None = None
    #: The local model's suggested words (``app.ingestion.parsers.vocabulary``)
    #: and whether the section the lines are in was typed by one.
    vocabulary: Vocabulary | None = None
    section_suggested: bool = False
    #: The section was typed through the vocabulary (suggested or learned).
    section_worded: bool = False
    #: Action phrases and headings of the rows nothing could type (their keys).
    unknown_actions: list[str] = field(default_factory=list)
    unknown_headings: list[str] = field(default_factory=list)
    #: Column headings of activity tables whose header could not be used.
    unknown_columns: list[str] = field(default_factory=list)
    #: Where the lines left out are (a row nothing typed, an unread amount),
    #: for the review queue's highlight.
    skipped_regions: list[tuple[int, tuple[float, float, float, float]]] = field(
        default_factory=list
    )
    #: Lines that look like entries (a date, an amount) under no table the
    #: parser could read: a table whose header it does not know.
    unread: list[tuple[int, tuple[float, float, float, float]]] = field(default_factory=list)
    #: The latest row of each side-by-side table (lines under it continue it).
    last_rows: dict[int, _ActivityRow] = field(default_factory=dict)

    @property
    def found_table(self) -> bool:
        return self.holdings_header or self.activity_header


#: A line naming the page it is on (``Page 2 of 4``, ``Statement page 3``).
_PAGE_LABEL: Final = re.compile(r"\bpage \d{1,4}(?: of \d{1,4})?\b")
#: A line saying the table above runs on (``Account Activity (continued)``;
#: normalised, so ``cont'd`` is ``cont d``).
_CONTINUED: Final = re.compile(r"\b(?:continued|cont d|contd)\b")
#: Words naming a list of entries: a line holding one is a table's heading
#: (``Other Transactions``, ``Miscellaneous Items``, ``Wire Transfers``),
#: never a running page header, whatever else it names, unless it is the
#: statement's title (:func:`_titled`).
_LIST_WORDS: Final = frozenset(
    {
        "activity",
        "activities",
        "additions",
        "adjustments",
        "charges",
        "checks",
        "contributions",
        "credits",
        "debits",
        "deposits",
        "disbursements",
        "distributions",
        "dividends",
        "entries",
        "fees",
        "items",
        "miscellaneous",
        "other",
        "payments",
        "purchases",
        "receipts",
        "sales",
        "subtractions",
        "trades",
        "transactions",
        "transfers",
        "withdrawals",
    }
)
#: List words naming all of an account's entries, as the statement's title
#: does (``Account Activity for XXXX-1234``), not one kind of them.
_ALL_ENTRIES_WORDS: Final = frozenset({"activity", "activities", "transactions"})
#: Words naming the statement itself (``Monthly Activity Statement``): a
#: title may say them beside all-entries words and title filler
#: (:func:`_titled`); beside any other word they make no title
#: (``Miscellaneous Items Report``).
_TITLE_WORDS: Final = frozenset({"statement", "statements", "report"})
#: Words a title says beside all-entries words and the account's number or
#: the page (``Your Account Activity for XXXX-1234``).
_TITLE_FILLER: Final = frozenset(
    {
        "account",
        "accounts",
        "annual",
        "ending",
        "for",
        "in",
        "monthly",
        "no",
        "number",
        "of",
        "on",
        "quarterly",
        "the",
        "your",
    }
)
#: What makes a line of a later page's running header
#: (:meth:`~BrokerageStatementParser._running_line`).
_RunningLine = Literal["band", "named", "dated", "continued", "plain"]


def _titled(norm: str, named: bool) -> bool:
    """A (normalised) line holding a list word is the statement's title, no
    table's heading: it says only all-entries words, words naming the
    statement itself (:data:`_TITLE_WORDS`), title filler and the account's
    number or the page, and it names the statement (``Monthly Activity
    Statement``, ``Statement of Transactions - Account XXXX-1234``) or the
    account or the page (``named``: ``Account Activity for XXXX-1234``,
    ``Account Transactions, Page 2``). Any other word makes it a heading
    (``Other Transactions (Page 2)``, ``Securities Lending Activity - Account
    XXXX-1234``, ``Miscellaneous Items Report``, ``Transactions After
    Statement Date``).
    """
    words = _PAGE_LABEL.sub(" ", norm).split()
    return (named or bool(_TITLE_WORDS.intersection(words))) and all(
        word in _ALL_ENTRIES_WORDS
        or word in _TITLE_WORDS
        or word in _TITLE_FILLER
        or any(ch.isdigit() for ch in word)
        or set(word) == {"x"}
        for word in words
    )


def _key(norm: str) -> str:
    """A normalised text as header bands are compared: digits alike."""
    return re.sub(r"\d", "0", norm)


def _band_key(line: ViewLine) -> str:
    """A line as a page's header band is compared: normalised, digits alike."""
    return _key(line.norm)


def _page_bands(lines: Sequence[ViewLine]) -> dict[int, frozenset[str]]:
    """Each page's header band: its first :data:`HEADING_TOP_LINES` lines above
    its first line that prints an amount or is led by a date (a table row, a
    summary figure), as :func:`_band_key` gives them.

    A line in the bands of two pages is printed at the top of both: a running
    page header (the client's name, the institution's), not a heading. Each
    cell of a band line of several cells is in the band too (``Example
    Brokerage | Statement for March 2024`` on one page, ``Example
    Brokerage`` alone on the next).
    """
    bands: dict[int, set[str]] = {}
    seen: dict[int, int] = {}
    ended: set[int] = set()
    for line in lines:
        band = bands.setdefault(line.page, set())
        seen[line.page] = position = seen.get(line.page, 0) + 1
        if line.page in ended:
            continue
        if (
            position > HEADING_TOP_LINES
            or find_amounts(line.text)
            or numeric_parts(line.cells[0].text) is not None
        ):
            ended.add(line.page)
            continue
        band.add(_band_key(line))
        if len(line.cells) > 1:
            band.update(_key(normalise(cell.text)) for cell in line.cells)
    return {page: frozenset(band) for page, band in bands.items()}


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Where a failed check comes from: sections, summary lines, a trade's own figures
# ---------------------------------------------------------------------------


def _total_scope(total: str, heading: str) -> Literal["all", "table", "buy", "sell"] | None:
    """The rows of its section a (normalised) ``Total ...`` line sums, if it says.

    ``all``: the label is the section's heading (``Total Deposits & EFT In``
    under *Deposits*, ``Net Securities Bought & Sold``); ``table``: it names
    nothing (``Total``, ``Total this period``), so it sums the table it
    ends; ``buy``/``sell``: it names the purchases or the sales of the
    section (``Total Securities Bought``). Any other label sums rows the
    words alone do not pick out (``Total Federal Taxes Withheld``): None.
    """
    words = total.split()
    while words and words[0] in _TOTAL_WORDS:
        words.pop(0)
    rest = " ".join(words)
    if rest in _WHOLE_SECTION:
        return "table"
    if heading and (rest == heading or rest.startswith(f"{heading} ")):
        return "all"
    named = set(words)
    known = set(heading.split())
    if named & _BUY_WORDS and not named & _SELL_WORDS and named - _BUY_WORDS <= known:
        return "buy"
    if named & _SELL_WORDS and not named & _BUY_WORDS and named - _SELL_WORDS <= known:
        return "sell"
    return None


def _agree(computed: Decimal, printed: Decimal) -> bool:
    """The rows add up to the printed total (a table printing no signs: in size)."""
    return abs(computed - printed) <= TOLERANCE or abs(abs(computed) - abs(printed)) <= TOLERANCE


def _checked(
    label: str, rows: Sequence[int], computed: Decimal, printed: Decimal, page: int | None
) -> SectionCheck:
    return SectionCheck(
        label=label[:SECTION_LABEL_MAX],
        rows=list(rows),
        status="ok" if _agree(computed, printed) else "mismatch",
        printed=printed,
        computed=computed,
        difference=computed - printed,
        page=page,
    )


def section_checks(
    activity: Sequence[InvestmentActivity],
    rows: Sequence[_ActivityRow],
    totals: Sequence[_SectionTotal],
) -> list[SectionCheck]:
    """Each section's rows against the totals the statement prints under them.

    ``rows[i]`` is the collected row ``activity[i]`` was read from. A
    section's rows are summed as the statement prints them (a transfer of
    units by its value, although it moves no cash); the last total of each
    scope (:func:`_total_scope`) is the one checked. A section printing no
    usable total is listed ``unchecked``.
    """

    def value(index: int) -> Decimal:
        row = rows[index]
        if activity[index].type in ("transfer_in", "transfer_out") and row.printed is not None:
            return row.printed
        return activity[index].amount

    by_section: dict[int, list[int]] = {}
    for index, row in enumerate(rows):
        by_section.setdefault(row.section_id, []).append(index)
    out: list[SectionCheck] = []
    for section_id, indices in by_section.items():
        first = rows[indices[0]]
        heading = normalise(first.section_label or "")
        last: dict[tuple[str, int], tuple[_SectionTotal, list[int]]] = {}
        for total in totals:
            if total.section_id != section_id:
                continue
            scope = _total_scope(total.norm, heading)
            if scope == "all":
                last[("all", 0)] = (total, indices)
            elif scope == "table":
                covered = [i for i in indices if rows[i].table == total.table]
                last[("table", total.table)] = (total, covered)
            elif scope == "buy":
                covered = [i for i in indices if activity[i].type in ("buy", "reinvest")]
                last[("buy", 0)] = (total, covered)
            elif scope == "sell":
                last[("sell", 0)] = (total, [i for i in indices if activity[i].type == "sell"])
        found = [
            _checked(
                total.label,
                covered,
                sum_amounts(value(i) for i in covered),
                total.amount,
                total.page,
            )
            for total, covered in last.values()
            if covered
        ]
        if not found:
            found.append(
                SectionCheck(
                    label=(first.section_label or "Activity")[:SECTION_LABEL_MAX],
                    rows=indices,
                    status="unchecked",
                    computed=sum_amounts(value(i) for i in indices),
                    page=first.page,
                )
            )
        out.extend(found)
    return [c.model_copy(update={"source": "total"}) if c.printed is not None else c for c in out][
        :MAX_SECTION_CHECKS
    ]


def _flow_key(label: str) -> str:
    """A cash-flow line or a section heading as compared: no ``and``, no footnote letter."""
    words = [w for w in normalise(label).split() if w != "and"]
    if len(words) > 1 and len(words[-1]) == 1 and words[-1].isalpha():
        words.pop()
    return " ".join(words)


def _flow_rows(key: str, activity: Sequence[InvestmentActivity]) -> list[int]:
    """The activity rows a cash-flow summary line (its :func:`_flow_key`) restates."""
    words = set(key.split())
    if words & _BUY_WORDS and not words & _SELL_WORDS:
        return [i for i, a in enumerate(activity) if a.type in ("buy", "reinvest")]
    if words & _SELL_WORDS and not words & _BUY_WORDS:
        return [i for i, a in enumerate(activity) if a.type == "sell"]
    lead = key.split(" ", 1)[0]
    moved = words & _TRANSFER_WORDS
    way = words & {"in", "out"}
    out: list[int] = []
    for index, row in enumerate(activity):
        section = _flow_key(row.section or "")
        if not section:
            continue
        if lead == "margin":
            same = section.split(" ", 1)[0] == "margin"
        elif moved and way:
            # "Transfers In": the cash rows of "Securities Transferred In".
            named = set(section.split())
            same = bool(named & _TRANSFER_WORDS) and way <= named and bool(row.amount)
        else:
            same = section == key or section.startswith(f"{key} ") or key.startswith(f"{section} ")
        if same:
            out.append(index)
    return out


def localise(
    statement: InvestmentStatement,
    check: InvestmentReconciliation,
    sections: Sequence[SectionCheck] = (),
) -> InvestmentReconciliation:
    """``check`` with what the statement itself says about each row (SECURITY.md C12).

    A statement prints more than its balances: a total under each activity
    section and a cash-flow summary restating every section. When a check
    fails, these say where:

    * **activity** (the ``cash`` check failed): a row is vouched for when
      its section's rows add up to the section's printed total or to the
      cash-flow summary's line for it (``source = summary``, compared where
      the section's own total does not already vouch for them). Where they
      do not add up and exactly one row is the whole difference (a row too
      many), that row alone is in doubt and the others are vouched for. A
      row moving cash that the cash-flow summary does not list at all, when
      that summary itself bridges opening to closing cash, is not vouched
      for whatever its section's total says (value moved between accounts,
      not core cash). A row moving no cash cannot be what broke the check.
      Everything else is doubtful; when no section of the doubtful rows
      prints a total and one of them is the whole cash difference, only
      that row. When every section adds up and the cash still does not, the
      rows are trusted only if the statement itself names cash flow that was
      not read (a bridging cash-flow summary with a line no row was read
      for); otherwise the one row that is the whole difference is in doubt,
      else every row moving cash.
    * **positions**: those whose quantity x price is not their market value;
      all of them when they do not add up to the printed holdings total, or
      when holdings + cash is not the account value and no holdings total is
      printed to vouch for their sum.

    ``doubtful_activity``/``doubtful_positions`` list the rows left in doubt
    (empty: the difference is the statement's own, no row is to blame);
    ``unread_summary_lines`` the cash-flow lines no row was read for. A
    statement that reconciles, or that nothing can be checked against, is
    returned with the sections only.
    """
    activity = statement.activity
    found = list(sections)
    flow = [f for f in statement.figures if f.group == "cash_flow"]
    opening = next((f.amount for f in flow if f.role == "opening"), None)
    closing = next((f.amount for f in flow if f.role == "closing"), None)
    lines = [f for f in flow if f.role == "item" and not f.detail and f.amount is not None]
    summary_difference: Decimal | None = None
    if opening is not None and closing is not None and lines:
        summary_difference = opening + sum_amounts(f.amount or Decimal(0) for f in lines) - closing
    listed: set[int] = set()
    unread: list[str] = []
    for figure in lines:
        key = _flow_key(figure.label)
        if any(_starts(key, restated) for restated in _FLOW_RESTATED):
            continue
        rows = _flow_rows(key, activity)
        if not rows:
            if figure.amount:
                unread.append(figure.label)
            continue
        listed.update(rows)
        if figure.amount is None or set(rows) <= {
            i for c in found if c.status == "ok" for i in c.rows
        }:
            continue  # their own totals already vouch for them
        if any(c.printed == figure.amount and c.rows == rows for c in found):
            continue  # the same figure as the section's own total
        computed = sum_amounts(activity[i].amount for i in rows)
        found.append(
            _checked(figure.label, rows, computed, figure.amount, figure.page).model_copy(
                update={"source": "summary"}
            )
        )
    update: dict[str, Any] = {
        "sections": found[:MAX_SECTION_CHECKS],
        "summary_cash_difference": summary_difference,
        "unread_summary_lines": unread[:MAX_SECTION_CHECKS],
    }
    if check.status != "mismatch":
        return check.model_copy(update=update)
    doubtful_activity: list[int] = []
    if "cash" in check.failed:
        vouched = {i for c in found if c.status == "ok" for i in c.rows}
        for section in found:
            surplus = _surplus(activity, section.rows, section.difference)
            if section.status == "mismatch" and surplus is not None:
                vouched |= set(section.rows) - {surplus}
        bridges = summary_difference is not None and abs(summary_difference) <= TOLERANCE
        unlisted = {i for i in range(len(activity)) if bridges and i not in listed}
        doubtful_activity = [
            i
            for i, row in enumerate(activity)
            if row.amount and (i in unlisted or i not in vouched)
        ]
        moving = [i for i, row in enumerate(activity) if row.amount]
        totalled = {i for c in found if c.status != "unchecked" for i in c.rows}
        if not doubtful_activity and not (bridges and unread):
            # Every section adds up and yet the cash does not, and the
            # statement names no cash flow left unread: a row too many whose
            # section's total counts it (one row that is the whole
            # difference), else nothing says which rows to trust.
            surplus = _surplus(activity, moving, check.cash_difference)
            doubtful_activity = [surplus] if surplus is not None else moving
        elif not (set(doubtful_activity) & (totalled | unlisted)):
            # No printed total says where: one row that is the whole difference.
            surplus = _surplus(activity, doubtful_activity, check.cash_difference)
            if surplus is not None:
                doubtful_activity = [surplus]
    doubtful_positions: Sequence[int] = check.position_mismatches
    if "holdings_total" in check.failed or (
        "total_value" in check.failed and "holdings_total" not in check.checks
    ):
        doubtful_positions = range(len(statement.positions))
    update["doubtful_activity"] = doubtful_activity
    update["doubtful_positions"] = list(doubtful_positions)
    return check.model_copy(update=update)


def _surplus(
    activity: Sequence[InvestmentActivity], rows: Sequence[int], difference: Decimal | None
) -> int | None:
    """The one row of ``rows`` whose cash is the whole ``difference`` (a row too many)."""
    if not difference:
        return None
    same = [i for i in rows if abs(activity[i].amount - difference) <= TOLERANCE]
    return same[0] if len(same) == 1 else None


#: A line this many line heights under a row no longer continues it.
_CONTINUATION_GAP: Final = 1.2
_MAX_ROW_NOTES: Final = 4
_MEMO_MAX: Final = 300


def _region(regions: list[tuple[int, tuple[float, float, float, float]]], line: ViewLine) -> None:
    """Keep where ``line`` is (bounded: the highlight shows the first ones)."""
    if len(regions) < MAX_SECTION_CHECKS:
        regions.append((line.page, line.bbox))


def _nearest_date(text: str, styles: Sequence[DateStyle], period: tuple[date, date]) -> date | None:
    """A yearless ``MM/DD`` (``DD/MM``) placed in the year nearest ``period``.

    For a date :func:`~app.ingestion.parsers.text.infer_year` would not
    place (more than a few days after the period, or long before it). None
    for a date that prints its year, or no numeric date.
    """
    parts = numeric_parts(text)
    if parts is None or parts[2] is not None:
        return None
    month, day = (parts[1], parts[0]) if "dm" in styles and "md" not in styles else parts[:2]
    best: tuple[int, date] | None = None
    for year in range(period[0].year - 1, period[1].year + 2):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if period[0] <= candidate <= period[1]:
            return candidate
        gap = min(abs((candidate - period[0]).days), abs((candidate - period[1]).days))
        if best is None or gap < best[0]:
            best = (gap, candidate)
    return best[1] if best is not None else None


def _remember(keys: list[str], text: str) -> None:
    """Keep the :func:`phrase_key` of ``text`` once (at most :data:`MAX_PHRASES`)."""
    key = phrase_key(text)
    if key and key not in keys and len(keys) < MAX_PHRASES:
        keys.append(key)


def _memo(row: _ActivityRow) -> str | None:
    """The row's description, with what its own words add (``... (Return of capital)``)
    and the notes printed under it (``...; Wash sale of ...``)."""
    head = row.description or row.note or ""
    if row.note and row.description:
        head = f"{row.description} ({row.note})"
    return "; ".join(part for part in (head, *row.notes) if part)[:_MEMO_MAX] or None


class BrokerageStatementParser(DocumentParser):
    """Generic brokerage statement: holdings and activity tables, one or more accounts.

    **Accounts.** Where each account's pages are is found by
    :func:`app.ingestion.parsers.accounts.segment` (account headings in a
    page's header band, also beside amounts; the account index's names and
    types). When the headings name one account (or none) the document is
    one statement, read exactly as a single-account statement always was
    (its last four digits from the first account number found, else from
    the heading). When they name two or more, each account is its own
    statement (own last four digits, name, type and subtype from the
    index, pages, positions, activity, cash, totals and reconciliation):

    * a page whose headings all name one account belongs to it entirely (a
      heading repeated at the top or the bottom of each page);
    * on a page naming several, the lines before the first heading continue
      the account of the page before, and each heading starts its account;
    * a page with no heading continues the account before it; the pages
      before the first heading (an overview of all accounts) give only the
      period, currency and "as of" date every account falls back on, and a
      holdings or activity row there belongs to no account (skipped,
      ``rows_outside_account``);
    * an account without a holdings or activity table is read as a bank
      account by :attr:`bank_parser` (a combined statement's checking and
      savings: a :class:`~app.ingestion.sandbox.protocol.Statement`), else it
      gives no statement (``account_without_tables``); at most
      :data:`MAX_STATEMENTS` accounts.

    Misplaced lines cannot pass silently: they break that account's
    reconciliation, and the importer holds a statement that does not
    reconcile.
    """

    name: ClassVar[str] = "brokerage_statement_generic"
    version: ClassVar[str] = "8"
    kind: ClassVar[Literal["statement", "payslip", "receipt"]] = "statement"
    display_name: ClassVar[str | None] = None
    #: Phrases on the first page that identify the institution (its name first).
    fingerprints: ClassVar[tuple[str, ...]] = ()
    #: Every fingerprint phrase must be on page 1 (not only the name): for an
    #: institution whose name other documents print (a payee, a transfer).
    all_fingerprints: ClassVar[bool] = False
    #: Other names the institution goes by in the PDF's title or author.
    metadata_names: ClassVar[tuple[str, ...]] = ()
    #: Phrases anywhere in the document that name the institution (a
    #: subsidiary in a footer) when page 1 does not (:meth:`named`).
    name_anchors: ClassVar[tuple[str, ...]] = ()
    #: The layout's own wording, anywhere in the document: required beside a
    #: name found only in the metadata or by a name anchor.
    anchors: ClassVar[tuple[str, ...]] = ()
    account_type: ClassVar[str] = "brokerage"
    default_currency: ClassVar[str | None] = None
    date_styles: ClassVar[tuple[DateStyle, ...]] = ()
    period_styles: ClassVar[tuple[DateStyle, ...]] = FULL_STYLES
    base: ClassVar[float] = conf.GENERIC
    period_labels: ClassVar[tuple[str, ...]] = (
        "statement period",
        "for the period",
        "statement from",
        "period",
    )
    #: Summary labels in the order they are tried on a line (longest first);
    #: ``ignore`` consumes a label no slot takes (a beginning account value,
    #: or the change in it, is not the account value); ``cash`` is a cash
    #: balance: the closing cash, and the opening cash too when the line has
    #: a column of each under a ``This Period | Last Period`` header.
    summary_labels: ClassVar[tuple[tuple[str, str], ...]] = (
        ("beginning account value", "ignore"),
        ("change in account value", "ignore"),
        ("change in portfolio value", "ignore"),
        ("beginning net value", "ignore"),
        ("beginning portfolio value", "ignore"),
        ("beginning cash balance", "cash_opening"),
        ("opening cash balance", "cash_opening"),
        ("starting cash balance", "cash_opening"),
        ("cash balance beginning", "cash_opening"),
        ("ending cash balance", "cash_closing"),
        ("closing cash balance", "cash_closing"),
        ("cash balance ending", "cash_closing"),
        ("beginning core account balance", "cash_opening"),
        ("ending core account balance", "cash_closing"),
        ("beginning sweep balance", "cash_opening"),
        ("ending sweep balance", "cash_closing"),
        ("cash and sweep balance", "cash"),
        ("cash sweep balance", "cash"),
        ("sweep balance", "cash"),
        ("cash balance", "cash"),
        ("total holdings value", "positions_value"),
        ("total market value", "positions_value"),
        ("total securities value", "positions_value"),
        ("total holdings", "positions_value"),
        ("holdings value", "positions_value"),
        ("ending account value", "total_value"),
        ("ending net value", "total_value"),
        ("total account value", "total_value"),
        ("total portfolio value", "total_value"),
        ("account value", "total_value"),
    )
    skip_prefixes: ClassVar[tuple[str, ...]] = ("page ", "continued", "subtotal")
    #: Read an activity table's description as the action when it has no
    #: action column (always; the generic parser only when the table also
    #: names a security, a symbol or a quantity, so a bank table stays a bank's).
    description_types: ClassVar[bool] = False
    #: Holdings groups whose rows are the account's cash (a core/sweep money
    #: market position): beginning and ending market values are the opening
    #: and closing cash, they are no position, and the printed holdings totals
    #: include them (subtracted for the holdings check).
    cash_groups: ClassVar[tuple[str, ...]] = ()
    #: Headings (normalised prefixes) of tables not to read (their rows would
    #: count the same money twice), until one of ``section_headings``.
    ignored_tables: ClassVar[tuple[str, ...]] = ()
    section_headings: ClassVar[tuple[str, ...]] = ()
    #: Reads an account of a document of several that has no brokerage table
    #: but a bank transaction table (a combined statement's checking account).
    bank_parser: ClassVar[type[TableStatementParser]] = TableStatementParser

    # -- detection ----------------------------------------------------------------

    @staticmethod
    def holdings_columns(line: ViewLine) -> list[_Column[HoldingRole]] | None:
        return BrokerageStatementParser._holdings_from(_header_cells([line]))

    @staticmethod
    def _holdings_from(cells: Sequence[_HeaderCell]) -> list[_Column[HoldingRole]] | None:
        found, _ = _columns(cells, HOLDING_HEADERS, "other")
        roles = {c.role for c in found}
        if {"quantity", "value"} <= roles and roles & {"symbol", "name"}:
            return found
        return None

    @classmethod
    def activity_columns(cls, line: ViewLine) -> list[_Column[ActivityRole]] | None:
        return cls._activity_from(_header_cells([line]))

    @classmethod
    def _activity_from(
        cls,
        cells: Sequence[_HeaderCell],
        *,
        cash_tables: bool = False,
        typeless: bool = False,
        headers: Mapping[str, ActivityRole] = ACTIVITY_HEADERS,
    ) -> list[_Column[ActivityRole]] | None:
        found, _ = _columns(cells, headers, "other")
        roles = {c.role for c in found}
        if typeless and {"date", "amount"} <= roles and not roles & {"type", "description"}:
            # A margin interest table: a period and what it charged, nothing else.
            return found
        if "date" not in roles and "settle" in roles:
            # Only a settlement (posting) date: it dates the row.
            found = [_Column("date", c.x0, c.x1) if c.role == "settle" else c for c in found]
            roles = {c.role for c in found}
        if (
            "type" not in roles
            and "description" in roles
            and (cls.description_types or cash_tables or roles & {"symbol", "name", "quantity"})
        ):
            # "You Bought", "Dividend Received": the description is the action.
            found = [_Column("type", c.x0, c.x1) if c.role == "description" else c for c in found]
            roles = {c.role for c in found}
        if {"date", "type", "amount"} <= roles:
            return found
        return None

    @classmethod
    def _parts(
        cls,
        cells: Sequence[_HeaderCell],
        *,
        cash_tables: bool = False,
        typeless: bool = False,
        headers: Mapping[str, ActivityRole] = ACTIVITY_HEADERS,
    ) -> list[_Part] | None:
        """The activity tables a header names: one, or several side by side.

        A header whose cells repeat one group (``Date | Reference |
        Transaction Description | Transaction Total`` twice) is that many
        tables on the same lines; each reads the cells of its own half (from
        midway between the groups).
        """
        count = len(cells)
        keys = [c.key for c in cells]
        for size in range(2, count // 2 + 1):
            if count % size or any(keys[i] != keys[i % size] for i in range(count)):
                continue
            groups = [list(cells[i : i + size]) for i in range(0, count, size)]
            parts: list[_Part] = []
            for number, group in enumerate(groups):
                columns = cls._activity_from(
                    group, cash_tables=cash_tables, typeless=typeless, headers=headers
                )
                if columns is None:
                    return None
                last = number == len(groups) - 1
                lo = -_FAR if number == 0 else (groups[number - 1][-1].x1 + group[0].x0) / 2
                hi = _FAR if last else (group[-1].x1 + groups[number + 1][0].x0) / 2
                parts.append(_Part(columns, lo, hi))
            return parts
        columns = cls._activity_from(
            cells, cash_tables=cash_tables, typeless=typeless, headers=headers
        )
        return [_Part(columns, -_FAR, _FAR)] if columns is not None else None

    def _title(self, line: ViewLine) -> bool:
        """A heading (section, asset group, ignored table): never the top line of a header."""
        if len(line.cells) > 2 or find_amounts(line.text):
            return False
        norm = line.norm
        return (
            section_rule(norm) is not None
            or self._group(norm, prefix=True) is not None
            or any(_starts(norm, p) for p in self.ignored_tables)
        )

    def _header_at(
        self,
        lines: Sequence[ViewLine],
        index: int,
        *,
        cash_tables: bool = False,
        typeless: bool = False,
        vocabulary: Vocabulary | None = None,
    ) -> _Header | None:
        """A table header starting at ``lines[index]`` over up to three lines.

        A header over several lines (``Price`` / ``Per Unit``, ``Settlement``
        / ``Date``, a date under ``Ending Market Value``) is used when it
        names more known columns than fewer of its lines; a later line with
        an amount is a row, never header, and a heading (a section, an asset
        group) is never a header's top line; nor is a short top line (one or
        two cells) that names no column the header's other lines do not (an
        unknown heading, a running page header: its words would only add or
        widen ``other`` columns). The other lines are compared as they are,
        so a page's running header over an unknown heading over the columns
        (``Jane Example`` / ``ATM Withdrawals`` / ``Date | Description |
        Amount``) is no header either and the heading stays one, as it is
        anywhere else on a page.
        """
        first = lines[index]
        if find_amounts(first.text):
            return None
        best: tuple[int, _Header] | None = None
        for count in (1, 2, 3):
            group = lines[index : index + count]
            if len(group) < count or group[-1].page != first.page:
                break
            if count > 1:
                if find_amounts(group[-1].text):
                    break
                if self._title(group[-2]):
                    break  # a heading starts what follows, it heads no column
            found = self._header_of(
                group, cash_tables=cash_tables, typeless=typeless, vocabulary=vocabulary
            )
            if found is not None and (best is None or found[0] > best[0]):
                best = found
        if best is None:
            return None
        used = best[1].used
        if used > 1 and len(first.cells) <= 2:
            below = self._header_of(
                lines[index + 1 : index + used],
                cash_tables=cash_tables,
                typeless=typeless,
                vocabulary=vocabulary,
            )
            if below is not None and below[1].known_roles == best[1].known_roles:
                # The top line names no column ("Electronic Funds Transfers",
                # a running page header "Jane Example - Account XXXX-1234"
                # over "Date | Description | Amount"): a heading, not header.
                return None
        return best[1]

    def _header_of(
        self,
        group: Sequence[ViewLine],
        *,
        cash_tables: bool,
        typeless: bool = False,
        vocabulary: Vocabulary | None = None,
    ) -> tuple[int, _Header] | None:
        """``(known columns named, header)`` that exactly ``group``'s lines make, if any.

        An activity header is read with the parser's own column words and
        those the household learned; one that names no table that way is
        read once more with the local model's suggested column headings
        (``vocabulary.columns``), and its table is then marked as read
        through a suggestion (:attr:`_Part.suggested`).
        """
        cells = _header_cells(group)
        holdings = self._holdings_from(cells)
        if holdings is not None:
            known = sum(1 for c in cells if header_key(c.parts, HOLDING_HEADERS) is not None)
            return known, _Header(holdings, None, len(group))
        headers = _activity_headers(vocabulary, suggestions=False)
        parts = self._parts(cells, cash_tables=cash_tables, typeless=typeless, headers=headers)
        if parts is None and vocabulary is not None and vocabulary.columns:
            headers = _activity_headers(vocabulary, suggestions=True)
            parts = self._parts(cells, cash_tables=cash_tables, typeless=typeless, headers=headers)
            if parts is not None:
                own = _activity_headers(vocabulary, suggestions=False)
                named = {k: k for k in vocabulary.columns if k not in own}
                key = next((k for c in cells if (k := header_key(c.parts, named))), "")
                parts = [replace(p, suggested=f"{COLUMN_PREFIX}{key}") for p in parts]
        if parts is None:
            return None
        known = sum(1 for c in cells if header_key(c.parts, headers) is not None)
        return known, _Header(None, parts, len(group))

    def _note_unknown_columns(
        self, lines: Sequence[ViewLine], index: int, collected: _Collected
    ) -> bool:
        """Remember the column headings of a header the parser cannot use; True: it is one.

        ``lines[index]`` is no header of a table the parser reads; it is
        taken for the header of one it does not when it holds three or more
        cells without an amount, at least one of them a column word the
        parser knows, and the line under it is an entry (a date, then an
        amount). The cells it does not know are what the optional local
        model may be asked about (``ParseInfo.unknown_columns``).
        """
        line = lines[index]
        if find_amounts(line.text) or self._title(line) or index + 1 >= len(lines):
            return False
        cells = [piece for c in line.cells for piece in split_header_cell(c)]
        if not 3 <= len(cells) <= _MAX_UNKNOWN_HEADER or _date_shaped(cells[0].text):
            return False
        below = lines[index + 1]
        if (
            below.page != line.page
            or len(below.cells) < 2
            or not _date_shaped(below.cells[0].text)
            or not find_amounts(below.text)
        ):
            return False
        headers = _activity_headers(collected.vocabulary, suggestions=True)
        roles = [header_key((c.text,), headers) for c in cells]
        if all(role is None for role in roles) or all(role is not None for role in roles):
            return False
        for cell, role in zip(cells, roles, strict=True):
            key = column_key(cell.text) if role is None else None
            if (
                key is not None
                and key not in collected.unknown_columns
                and len(collected.unknown_columns) < MAX_PHRASES
            ):
                collected.unknown_columns.append(key)
        return True

    def fingerprint_score(self, view: DocumentView) -> float:
        """Share of :attr:`fingerprints` on page 1 (the name also counts from :meth:`named`)."""
        if not self.fingerprints:
            return 0.0
        head = f" {view.head_text()} "
        hits = sum(1 for phrase in self.fingerprints[1:] if f" {normalise(phrase)} " in head)
        return (hits + (1 if self.named(view) else 0)) / len(self.fingerprints)

    def named(self, view: DocumentView) -> bool:
        """The institution names itself: on page 1, in the PDF's title or author, or by
        one of :attr:`name_anchors` anywhere, with an :attr:`anchors` phrase beside it.

        Its name may be only in a logo (an image): the document's metadata or
        the name of a subsidiary in a footer stands in for it, but only with
        the layout's own wording somewhere (a payee or transfer naming the
        institution on some page proves nothing).
        """
        if not self.fingerprints:
            return False
        name = self.fingerprints[0]
        if f" {normalise(name)} " in f" {view.head_text()} ":
            return True
        elsewhere = any(view.in_metadata(n) for n in (name, *self.metadata_names)) or any(
            view.has_phrase(p) for p in self.name_anchors
        )
        return elsewhere and any(view.has_phrase(p) for p in self.anchors)

    def _tables(self, lines: Sequence[ViewLine]) -> tuple[bool, bool]:
        """``(holdings table, activity table)`` among ``lines`` (cash-only tables not counted)."""
        holdings = activity = False
        for index in range(len(lines)):
            found = self._header_at(lines, index)
            if found is not None:
                holdings = holdings or found.holdings is not None
                activity = activity or found.parts is not None
                if holdings and activity:
                    break
        return holdings, activity

    def detect(self, view: DocumentView) -> float:
        if not self.fingerprints:
            holdings, activity = self._tables(view.lines)
            # Above the bank-statement parsers (0.4/0.45) for the same activity table.
            if holdings:
                return 0.7 if activity else 0.6
            return 0.5 if activity else 0.0
        if not self.named(view):
            return 0.0  # the institution's own name (the first phrase) must be there
        share = self.fingerprint_score(view)
        if self.all_fingerprints and share < 1:
            return 0.0
        holdings, activity = self._tables(view.lines)
        score = 0.5 + 0.4 * share
        return round(score + 0.1 if holdings or activity else score * 0.6, 4)

    # -- accounts -----------------------------------------------------------------

    def _sections(
        self, view: DocumentView, vocabulary: Vocabulary | None = None
    ) -> tuple[list[_Collected], _Collected | None, int, list[str]]:
        """``(accounts, preamble, accounts dropped, accounts the index lists that
        have no pages)`` (class doc); one account: no preamble."""
        split = segment(view.lines)
        bands = _page_bands(view.lines)
        if len(split.accounts) < 2:
            single = _Collected(bands=bands, vocabulary=vocabulary)
            self._read(view.lines, single)
            if single.summary.last4 is None and split.accounts:
                single.summary.last4 = split.accounts[0].last4
            single.info = split.info(single.summary.last4)
            # An index naming several accounts where the pages name one: the
            # others were not found.
            absent = [
                last4
                for last4 in split.index
                if len(split.index) > 1 and last4 != single.summary.last4
            ]
            return [single], None, 0, absent
        preamble = _Collected(bands=bands, vocabulary=vocabulary)
        self._read(split.preamble, preamble)
        accounts: list[_Collected] = []
        for account in split.accounts:
            collected = _Collected(
                account=account, info=account.info, bands=bands, vocabulary=vocabulary
            )
            self._read(account.lines, collected)
            collected.summary.last4 = account.last4
            accounts.append(collected)
        kept = accounts[:MAX_STATEMENTS]
        found = {account.last4 for account in split.accounts}
        absent = [last4 for last4 in split.index if last4 not in found]
        return kept, preamble, len(accounts) - len(kept), absent

    # -- collecting -----------------------------------------------------------------

    def _scan_summary(self, line: ViewLine, summary: _Summary) -> None:
        norm = line.norm
        if not find_amounts(line.text):
            current = _index_of(norm, ("this period", "current period"))
            previous = _index_of(norm, ("last period", "prior period", "previous period"))
            if current is not None and previous is not None:
                summary.current_first = current < previous
        for label, slot in self.summary_labels:
            end = label_end(line.text, label)
            if end is None:
                continue
            # ``Ending cash balance - $2,310.40``: the dash is the label's.
            amounts = after_label(line.text[end:], [cell.text for cell in line.cells])
            if slot == "cash":
                self._cash_line(amounts, summary, line.text[end:])
            elif slot != "ignore" and amounts and getattr(summary, slot) is None:
                setattr(summary, slot, amounts[0])
            break
        summary.period.scan(line.text, self.period_labels)
        if summary.as_of_text is None:
            end = label_end(line.text, "as of")
            if end is not None:
                summary.as_of_text = line.text[end:]
        if summary.last4 is None:
            summary.last4 = summary_last4(line.text, masked_digits=False)
        if summary.currency is None:
            match = _CURRENCY.search(line.text)
            if match is not None:
                summary.currency = match[1].upper()

    @staticmethod
    def _cash_line(amounts: Sequence[Amount], summary: _Summary, rest: str = "") -> None:
        """A cash balance line: one amount is the closing cash; two under a
        ``This Period | Last Period`` header are the closing and opening cash.

        A dated one (``Cash balance on 03/01/2024 $5,000.00``) waits for the
        period (:func:`_dated_cash`): the earliest of several is the opening
        cash, the latest the closing cash.
        """
        if not amounts:
            return
        if len(amounts) == 1 and find_dates(rest, FULL_STYLES):
            if len(summary.cash_dated) < _MAX_DATED_CASH:
                summary.cash_dated.append((rest, amounts[0]))
            return
        if len(amounts) >= 2 and summary.current_first is not None:
            closing, opening = amounts[:2] if summary.current_first else amounts[1::-1]
            if summary.cash_opening is None:
                summary.cash_opening = opening
            if summary.cash_closing is None:
                summary.cash_closing = closing
            return
        if summary.cash_closing is None:
            summary.cash_closing = amounts[0]

    @staticmethod
    def _name_symbol(raw: str | None) -> tuple[str | None, str | None]:
        """``(symbol, name)`` from a name cell: the ticker in parentheses (``... (EXMPL)``).

        A holding's symbol is never guessed from the name's first word (a
        bond's issuer is no ticker).
        """
        if raw is None:
            return None, None
        matches = list(_PAREN_SYMBOL.finditer(raw))
        if not matches:
            return None, raw.strip() or None
        last = matches[-1]
        rest = f"{raw[: last.start()]} {raw[last.end() :]}"
        return last[1], " ".join(rest.split()) or None

    def _holding(
        self,
        line: ViewLine,
        cells: dict[HoldingRole, list[Cell]],
        group: str | None,
        name_x0: float | None = None,
    ) -> _HoldingRow | None:
        quantity = read_number(_unmarked(_text(cells.get("quantity"))))
        if quantity is None:
            return None
        raw_name = _text(_without_mark(cells.get("name"), name_x0)) or None
        name = raw_name
        symbol, cusip = _identifier(_text(cells.get("symbol")))
        symbol_column = symbol is not None or cusip is not None
        if not symbol_column:
            symbol, name = self._name_symbol(raw_name)
        if symbol is None and cusip is None and name is None:
            return None
        price_text = _unmarked(_text(cells.get("price")))
        return _HoldingRow(
            page=line.page,
            bbox=line.bbox,
            symbol=symbol,
            name=name,
            quantity=quantity,
            price=read_number(price_text) if price_text else None,
            value=read_amount(_unmarked(_text(cells.get("value")))),
            cost=read_amount(_unmarked(_text(cells.get("cost")))),
            ocr=_ocr(cells),
            begin=read_amount(_unmarked(_text(cells.get("begin")))),
            cusip=cusip,
            group=group,
            raw_name=raw_name,
            symbol_column=symbol_column,
            closed=symbol is not None and not symbol_column,
        )

    def _continue_holding(
        self, line: ViewLine, cells: dict[HoldingRole, list[Cell]], row: _HoldingRow
    ) -> bool:
        """A line under a holding that belongs to it.

        * ``CUSIP: 94136RAC4`` / ``ISIN: US0378331005``: the row's identifier
          (also after its name is complete);
        * a note (``— 30-day yield: 4.83%``): part of the row, read for nothing;
        * the rest of a wrapped security name (``EXAMPLE TOTAL MARKET`` /
          ``INDEX FUND (EXMPL)``, ``USD 1.00 (ZZPGL)``) while the name has no
          ``(SYMBOL)`` yet and no identifier line came: text in the name
          column only, directly under the row.
        """
        if not cells or not set(cells) <= {"name", "symbol"}:
            return False
        if line.page != row.page or line.bbox[1] - row.bbox[3] > 1.5 * line.height:
            return False
        text = line.text.strip()
        ident = _IDENT_LINE.fullmatch(text)
        if ident is not None:
            # Identified: the lines held back as a possible note are its name.
            self._join_name(row, "")
            value = ident[2].upper()
            if ident[1].lower() == "cusip" and is_cusip(value):
                row.cusip = row.cusip or value
            elif ident[1].lower() == "isin" and _ISIN.fullmatch(value):
                row.isin = row.isin or value
            row.closed = True
            row.bbox = (*row.bbox[:3], line.bbox[3])
            return True
        if text[:1] in _NOTE_MARKS:
            row.bbox = (*row.bbox[:3], line.bbox[3])
            return True
        extra = _text(cells.get("name"))
        if not extra or row.closed:
            return False
        if (
            row.symbol is None
            and row.cusip is None
            and row.isin is None
            and _sentence_under_capitals(row.raw_name, extra)
        ):
            # ``Valued from an independent estimate`` under a name in capitals
            # nothing identifies: a note on the holding, unless an identifier
            # or more of the name follows (then it is part of the name).
            row.notes.append(extra)
            row.bbox = (*row.bbox[:3], line.bbox[3])
            return True
        self._join_name(row, extra)
        row.bbox = (*row.bbox[:3], line.bbox[3])
        return True

    def _join_name(self, row: _HoldingRow, extra: str) -> None:
        """Append the held-back note lines and ``extra`` to the row's name."""
        parts = [*row.notes, extra] if extra else row.notes
        if not parts:
            return
        row.notes = []
        row.raw_name = " ".join([row.raw_name or "", *parts]).strip()[:200]
        if row.symbol_column:
            row.name = row.raw_name
        else:
            closed = row.closed
            row.symbol, row.name = self._name_symbol(row.raw_name)
            row.closed = closed or row.symbol is not None

    def _wraps_name(self, line: ViewLine, collected: _Collected) -> bool:
        """A line worded like a section heading that is the rest of a holding's name.

        Only a line that ends the open holding's name (it has no ``(SYMBOL)``
        yet) with its ``(SYMBOL)``: ``INCOME FUND (ZZSIX)``;
        :meth:`_continue_holding` decides (name column only, directly under
        the row) and joins it. Any other heading-worded line is the heading.
        """
        row, columns = collected.last_holding, collected.holdings_columns
        if row is None or columns is None or row.closed:
            return False
        text = line.text.strip()
        if not any(len(text) - m.end() <= 2 for m in _PAREN_SYMBOL.finditer(text)):
            # Without its (SYMBOL) the line is the heading it reads as: a
            # holding nothing identifies yet (a plain or leading-ticker name)
            # does not take the next section's title into its name.
            return False
        return self._continue_holding(line, _assign(line, columns), row)

    def _group(self, norm: str, *, prefix: bool = False) -> str | None:
        """The asset-class group a heading names (``prefix``: a ``Total <group> ...`` rest)."""
        if norm.endswith(" continued"):
            norm = norm[: -len(" continued")]
        for group in (*self.cash_groups, *ASSET_GROUPS):
            if norm == group or (prefix and norm.startswith(f"{group} ")):
                return group
        return None

    def _holdings_total(self, line: ViewLine, collected: _Collected) -> None:
        rest = line.norm[len("total") :].strip()
        if self._group(rest, prefix=True) is not None:
            return  # "Total Mutual Funds": a group's subtotal
        columns = collected.holdings_columns or []
        value = read_amount(_text(_assign(line, columns).get("value")))
        if value is None:
            amounts = find_amounts(line.text)
            value = amounts[-1] if amounts else None
        if value is None:
            return
        summary = collected.summary
        if any(_starts(rest, g) for g in GRAND_TOTALS):
            if collected.table_total != "grand" and (
                summary.positions_value is None or collected.table_total == "plain"
            ):
                summary.positions_value = value
            collected.table_total = "grand"
        elif summary.positions_value is None:
            summary.positions_value = value
            collected.table_total = "plain"

    def _activity(
        self,
        line: ViewLine,
        cells: dict[ActivityRole, list[Cell]],
        collected: _Collected,
        *,
        symbol_column: bool = False,
        part: int = 0,
        column_suggestion: str | None = None,
    ) -> None:
        date_text = _text(cells.get("date"))
        if collected.section == "margin":
            date_text = _range_end(date_text)
        if not date_text or not _date_shaped(date_text):
            self._continue_activity(line, cells, collected, part)
            return
        collected.last_rows.pop(part, None)
        type_text = _text(cells.get("type"))
        name = _text(cells.get("name"))
        symbol, cusip = _identifier(_text(cells.get("symbol")))
        quantity_text = _unmarked(_text(cells.get("quantity")))
        quantity = read_number(quantity_text) if quantity_text else None
        amount_text = _unmarked(_text(cells.get("amount")))
        amount = read_amount(amount_text)
        # A transfer moves units when the row names a security and a quantity.
        named = symbol or cusip or name or _text(cells.get("description"))
        moves = bool(named) and bool(quantity)
        kind = activity_type(
            type_text,
            moves_security=moves,
            units_sign=_sign(quantity),
            cash_sign=_sign(amount.value if amount is not None else None),
        )
        # Text in the action column naming no action (a description standing in
        # for the action column: "Example Index Fund") describes the row.
        described = kind is None and bool(type_text) and not named
        section = collected.section
        by_sign = False
        own = kind  # what the row's own words say, before its section or a suggestion
        note: str | None = None
        printed = amount.value if amount is not None else None
        if section == "income" and kind == "deposit":
            # "Return of capital", "Cash in lieu" under the income heading: money
            # a holding paid, not money the holder put in.
            kind, note = "dividend", type_text
        if section in ("transfer_in", "transfer_out"):
            # A securities transfer moves units, its amount is their value; a
            # row without units moves cash.
            inward = section == "transfer_in"
            if moves or (described and quantity):
                kind = "transfer_in" if inward else "transfer_out"
                amount = Amount(value=Decimal(0))
            else:
                kind = "deposit" if inward else "withdrawal"
        elif kind is None and section == "income":
            kind = "interest" if "interest" in normalise(type_text).split() else "dividend"
        elif kind is None and section in ("fee", "margin"):
            kind = "fee"
        elif kind is None and section is not None and section in _CASH_SECTIONS:
            kind, by_sign = _CASH_SECTIONS[section], True
        suggested = collected.section_suggested and kind != own
        suggestion = (
            f"{SECTION_PREFIX}{phrase_key(collected.section_label or '')}" if suggested else None
        )
        if kind is None and collected.vocabulary is not None:
            # Words in no vocabulary of the parser's own: what the household
            # learned they are, else the local model's suggestion (ADR-0010).
            hinted = collected.vocabulary.action(type_text)
            if hinted is not None and hinted[0] in SUGGESTED_TYPES:
                kind, suggested = cast(InvestmentActivityType, hinted[0]), hinted[1]
                # A broker's action words (the row names its security or has a
                # description of its own) may be remembered; the free text of
                # a cash table (a payee, a memo) is this document's only.
                wording = bool(named) and "@" not in type_text
                suggestion = (
                    f"{ACTION_PREFIX}{phrase_key(type_text)}" if suggested and wording else None
                )
        if kind is None:
            collected.unknown_activity += 1
            collected.skipped += 1
            _region(collected.skipped_regions, line)
            _remember(collected.unknown_actions, type_text)
            if not collected.section_worded and section is None and collected.section_label:
                _remember(collected.unknown_headings, collected.section_label)
            return
        if column_suggestion is not None and not suggested:
            # The table is read through a column heading the model suggested:
            # every row of it is a suggestion (the figures are the page's own).
            suggested, suggestion = True, column_suggestion
        if (
            amount is None
            and kind in ("transfer_in", "transfer_out")
            and quantity
            and amount_text.strip("-\u2013 ") == ""
        ):
            amount = Amount(value=Decimal(0))  # units moved, no cash ("-")
        if amount is None:
            collected.skipped += 1
            _region(collected.skipped_regions, line)
            return
        description = name or _text(cells.get("description"))
        if not description and section == "margin":
            description = "Margin interest"
        elif not description and described:
            description = type_text  # typed by its section, the text is its description
        elif not description and kind in CASH_MEMO_TYPES:
            # A deposit table's action ("Electronic Funds Transfer Received") is its memo.
            description = type_text
        price_text = _unmarked(_text(cells.get("price")))
        fees_text = _text(cells.get("fees"))
        row = collected.last_rows[part] = _ActivityRow(
            page=line.page,
            bbox=line.bbox,
            date_text=date_text,
            settle_text=_text(cells.get("settle")),
            type=kind,
            description=description,
            symbol=symbol,
            quantity=quantity,
            price=read_number(price_text) if price_text else None,
            amount=amount,
            fees=read_amount(fees_text) if fees_text else None,
            ocr=_ocr(cells),
            cusip=cusip,
            table=collected.tables,
            by_sign=by_sign,
            security_name=name or None,
            section_label=collected.section_label,
            section_id=collected.section_id,
            printed=printed,
            symbol_column=symbol_column,
            note=note,
            suggested=suggested,
            suggestion=suggestion,
            bottom=line.bbox[3],
        )
        collected.activity.append(row)

    @staticmethod
    def _continue_activity(
        line: ViewLine,
        cells: dict[ActivityRole, list[Cell]],
        collected: _Collected,
        part: int,
    ) -> None:
        """A line printed right under an activity row, with no date of its own.

        In the row's security-name column it is the rest of the name (the
        first such line: ``ETF``, a bond's coupon and maturity) or a remark
        on the trade; in its action or description column it is a note
        (``Wash sale of ...``, ``LT loss: ...``, ``Accrued Interest: ...``).
        Both are kept with the row; a line with a figure in the amount
        column, or further down the page, is something else.
        """
        row = collected.last_rows.get(part)
        if row is None or line.page != row.page or cells.get("date") or cells.get("amount"):
            return
        if line.bbox[1] - row.bottom > _CONTINUATION_GAP * max(line.height, 1.0):
            collected.last_rows.pop(part, None)
            return
        name = _text(cells.get("name"))
        note = _text(cells.get("type")) or _text(cells.get("description"))
        if not name and not note:
            return
        row.bottom = max(row.bottom, line.bbox[3])
        if name and row.security_name and not row.name_lines:
            if row.description == row.security_name:
                row.description = f"{row.description} {name}"[:SECURITY_NAME_MAX]
            row.security_name = f"{row.security_name} {name}"[:SECURITY_NAME_MAX]
            row.name_lines = 1
        elif name:
            note = f"{name} {note}".strip()
        if note and len(row.notes) < _MAX_ROW_NOTES:
            row.notes.append(note[:SECTION_LABEL_MAX])

    @staticmethod
    def _section_total(piece: ViewLine, part: _Part, collected: _Collected) -> None:
        """Keep a ``Total ...`` line of an activity table (:func:`_total_scope`)."""
        amount = read_amount(_unmarked(_text(_assign(piece, part.columns).get("amount"))))
        if amount is None:
            amounts = find_amounts(piece.text)
            amount = amounts[-1] if amounts else None
        words: list[str] = []
        for cell in piece.cells:
            if not any(ch.isalpha() for ch in cell.text):
                break  # the figures start here
            words.append(cell.text)
        label = " ".join(words).strip()
        if amount is None or not label or len(collected.totals) >= MAX_SECTION_CHECKS:
            return
        collected.totals.append(
            _SectionTotal(
                label=label[:SECTION_LABEL_MAX],
                norm=normalise(label),
                amount=amount.value,
                section_id=collected.section_id,
                table=collected.tables,
                page=piece.page,
            )
        )

    def _figure(self, line: ViewLine, collected: _Collected) -> None:
        """Keep a line of the statement's own summary tables (``StatementFigure``).

        ``Beginning Account Value`` opens the account-value bridge wherever
        it is printed; a summary heading (:data:`FIGURE_SECTIONS`) opens the
        other groups. A group ends at its closing figure
        (:data:`_FIGURE_END`), at the next heading or table, or with its page.
        """
        norm = line.norm
        if collected.figure_page is not None and line.page != collected.figure_page:
            collected.figure_group = None
        if any(_starts(norm, label) for label in VALUE_OPENING):
            collected.figure_group, collected.figure_x = "account_value", None
        group = collected.figure_group
        if group is None or len(collected.figures) >= MAX_FIGURES:
            return
        read = _labelled(line)
        if read is None:
            return
        label, values = read
        key = normalise(label)
        first = key.split(" ", 1)[0]
        role: Literal["opening", "closing", "total", "item"] = "item"
        if first in ("beginning", "opening", "starting"):
            role = "opening"
        elif first in ("ending", "closing"):
            role = "closing"
        elif first in _TOTAL_WORDS:
            role = "total"
        x0 = line.cells[0].x0
        if collected.figure_x is None or x0 < collected.figure_x:
            collected.figure_x = x0
        collected.figure_page = line.page
        collected.figures.append(
            StatementFigure(
                group=group,
                label=label[:SECTION_LABEL_MAX],
                role=role,
                detail=x0 - collected.figure_x > _INDENT,
                amount=values[0].value if values[0] is not None else None,
                year_to_date=(
                    values[1].value if len(values) > 1 and values[1] is not None else None
                ),
                page=line.page,
                bbox=line.bbox,
            )
        )
        ends = _FIGURE_END.get(group)
        if (role == "closing" and group in ("account_value", "cash_flow")) or (
            ends is not None and _starts(key, ends)
        ):
            collected.figure_group = None

    def _is_heading(self, line: ViewLine, phrases: tuple[str, ...]) -> bool:
        if not phrases or len(line.cells) > 2 or find_amounts(line.text):
            return False
        return any(_starts(line.norm, p) for p in phrases)

    @staticmethod
    def _section_heading(line: ViewLine) -> SectionKind | None:
        """The activity section a heading line starts (at most two cells, no amount)."""
        if len(line.cells) > 2 or find_amounts(line.text):
            return None
        return section_rule(line.norm)

    def _unknown_heading(self, lines: Sequence[ViewLine], start: int) -> bool:
        """The line right above a table header (``lines[start]``) is a heading no
        activity section names (``Electronic Funds Transfers``).

        A page's top line, a row or total above (an amount), a page number or
        ``continued`` line, or a known section's heading is not one.
        """
        if start <= 0:
            return False
        above, header = lines[start - 1], lines[start]
        if above.page != header.page or len(above.cells) > 2 or find_amounts(above.text):
            return False
        norm = above.norm
        if not any(ch.isalpha() for ch in norm) or section_rule(norm) is not None:
            return False
        if any(norm.startswith(prefix.strip()) for prefix in self.skip_prefixes):
            return False
        return not _date_shaped(above.cells[0].text)

    @staticmethod
    def _continued(
        lines: Sequence[ViewLine], start: int, header: _Header, collected: _Collected
    ) -> bool:
        """The table header at ``lines[start]`` continues the section's table on a
        later page under the page's running header.

        Its column roles are the section's latest header's, it is on a later
        page, and every line above it on its page (at most
        :data:`HEADING_TOP_LINES`) is a line of the page's running
        header (:meth:`_running_line`): printed in the header band of that page
        and of the page the section's latest header is on (:func:`_page_bands`),
        naming the account (an account heading, its number) or the page
        (``Page 2 of 4``), a date or period line, or a line saying the table
        runs on (``Account Activity (continued)``). Any other line is plain
        text (a figure too): a heading no section names (:meth:`_unknown_heading`),
        which starts a table of its own as it does anywhere on a page, unless a
        line of the same block names the account or the page (a name under
        ``Page 2 of 4`` is the page's header; a heading under it holds a list
        word, :data:`_LIST_WORDS`, and is no running header line at all, unless
        it is the statement's title, :func:`_titled`). Known limit: a heading
        without a list word in such a block (``Securities Lending`` under
        ``Account XXXX-1234``) is taken for the running header too.
        """
        previous = collected.section_header
        if previous is None or start <= 0:
            return False
        first = lines[start]
        if previous[0] != header.roles or first.page <= previous[1]:
            return False
        top = start
        while top > 0 and lines[top - 1].page == first.page:
            top -= 1
        block = lines[top:start]
        if not block or len(block) > HEADING_TOP_LINES:
            return False
        last4 = (
            collected.account.last4 if collected.account is not None else collected.summary.last4
        )
        kinds = [
            BrokerageStatementParser._running_line(line, previous[1], collected, last4)
            for line in block
        ]
        if None in kinds:
            return False
        return "plain" not in kinds or "named" in kinds

    @staticmethod
    def _running_line(
        line: ViewLine, header_page: int, collected: _Collected, last4: str | None
    ) -> _RunningLine | None:
        """What makes ``line`` a line of a later page's running header
        (:meth:`_continued`), ``plain`` for text that says nothing of it (also
        a figure, ``Total Account Value $6,025.00``); None: it is no running
        header line (a line holding a list word not printed in both header
        bands, unless it is the statement's title, :func:`_titled`; a band
        line that headed a table of its own on the section's header page)."""
        norm = line.norm
        if _CONTINUED.search(norm) is not None:
            # "Other Transactions (continued)": that table runs on, not this one.
            rest = _key(" ".join(_CONTINUED.sub(" ", norm).split()))
            return None if rest in collected.headings else "continued"
        here = collected.bands.get(line.page, frozenset())
        there = collected.bands.get(header_page, frozenset())
        key = _band_key(line)
        cells = [_key(normalise(c.text)) for c in line.cells] if len(line.cells) > 1 else []
        printed = (key in here and key in there) or (
            bool(cells) and all(k in here and k in there for k in cells)
        )
        band = printed and header_page not in collected.headings.get(key, ())
        named = (
            _PAGE_LABEL.search(norm) is not None
            or any(
                account_heading(t) is not None for t in (line.text, *(c.text for c in line.cells))
            )
            or (last4 is not None and any(number_last4(t) == last4 for t in line.text.split()))
        )
        if _LIST_WORDS.intersection(norm.split()) and not band and not _titled(norm, named):
            return None
        if named:
            return "named"
        if band:
            return "band"
        if find_amounts(line.text):
            # A figure (a value box printed on every page) says nothing of
            # the page; a row's date does not make it a period line.
            return "plain"
        if find_dates(line.text, FULL_STYLES):
            return "dated"
        return "plain"

    def _read(self, lines: Sequence[ViewLine], collected: _Collected) -> None:
        """Read one account's lines (or the whole document) into ``collected``.

        Inside an account with a holdings table or an activity table naming
        securities (a brokerage account), a cash table (date, description,
        amount: deposits, card activity, fees) is activity too, typed by its
        description and its section.
        """
        # Blocks printed side by side (a summary box beside a table) are read
        # one after the other (:mod:`app.ingestion.parsers.blocks`).
        lines = unfold_blocks(
            lines,
            lambda norm: section_rule(norm) is not None,
            lambda line: self._header_at([line], 0, vocabulary=collected.vocabulary) is not None,
        )
        cash_tables = any(self._tables(lines))
        index = 0
        while index < len(lines):
            line = lines[index]
            if (
                collected.figure_group == "cash_flow"
                and line.page == collected.figure_page
                and line.norm in _FLOW_HEADINGS
            ):
                # "CASH MANAGEMENT ACTIVITY" inside the cash-flow summary heads
                # its lines, it starts no section of activity.
                index += 1
                continue
            section = self._section_heading(line)
            if section is not None and self._wraps_name(line, collected):
                # "INCOME FUND (ZZSIX)" under "EXAMPLE STRATEGIC": a name, no heading.
                index += 1
                continue
            if section is not None and not self._is_heading(line, self.ignored_tables):
                index += 1
                collected.section = section
                collected.last_rows = {}
                collected.section_suggested = collected.section_worded = False
                collected.section_label = _heading_label(line.text)
                collected.section_id += 1
                collected.sub_headings = []
                collected.figure_group = figure_group(line.norm)
                collected.figure_x = None
                collected.figure_page = line.page
                collected.section_tables = 0
                collected.section_header = None
                collected.ignoring = False
                collected.holdings_columns = collected.activity_parts = None
                continue
            found = self._header_at(
                lines,
                index,
                cash_tables=cash_tables,
                typeless=collected.section == "margin",
                vocabulary=collected.vocabulary,
            )
            if (
                found is None
                and not collected.ignoring
                and collected.section != "skip"
                and self._note_unknown_columns(lines, index, collected)
            ):
                # The header of a table the parser cannot read: the table
                # above ends here, and the entries under it are reported as
                # unread rather than read through the wrong columns.
                collected.holdings_columns = collected.activity_parts = None
                index += 1
                continue
            if found is not None:
                index += found.used
                collected.figure_group = None
                if collected.ignoring:
                    collected.holdings_columns = collected.activity_parts = None
                elif found.holdings is not None:
                    collected.holdings_columns, collected.activity_parts = found.holdings, None
                    collected.holdings_header = True
                    collected.section = None
                    collected.section_suggested = collected.section_worded = False
                    collected.section_label = None
                    collected.section_id += 1
                    # A group heading right above its table's header ("Core Account").
                    above = lines[index - found.used - 1] if index - found.used > 0 else None
                    collected.group = (
                        self._group(above.norm, prefix=True)
                        if above is not None
                        and above.page == line.page
                        and len(above.cells) <= 2
                        and not find_amounts(above.text)
                        else None
                    )
                    collected.last_holding = None
                else:
                    start = index - found.used
                    if self._unknown_heading(lines, start) and not self._continued(
                        lines, start, found, collected
                    ):
                        # A heading of a table of its own: on its page it is
                        # no running page header (:meth:`_running_line`).
                        collected.headings.setdefault(_band_key(lines[start - 1]), set()).add(
                            lines[start].page
                        )
                        above = lines[start - 1]
                        again = (
                            collected.section_header is not None
                            and collected.section_header[0] == found.roles
                            and _alike(above.norm, collected.sub_headings)
                        )
                        if collected.section is None or (collected.section_tables and not again):
                            # After the section's own table, a heading no
                            # section names starts a table of its own: the
                            # section above does not type (or skip) its rows.
                            # (Right under the section's heading it is a
                            # sub-heading: a card's name, and so is a later
                            # heading worded like it over the same columns,
                            # the next card's. The section's header
                            # repeated on a later page under the page's running
                            # header continues its table.) The household may
                            # have learned what kind of section it is, or the
                            # local model have suggested it (ADR-0010): its
                            # rows are then typed by it, a suggestion's each
                            # marked as one.
                            label = _heading_label(above.text)
                            hinted = (
                                collected.vocabulary.section(label)
                                if collected.vocabulary is not None
                                else None
                            )
                            worded = hinted is not None and hinted[0] in SUGGESTED_SECTIONS
                            collected.section = (
                                cast(SectionKind, hinted[0]) if hinted and worded else None
                            )
                            collected.section_worded = worded
                            collected.section_suggested = bool(hinted and worded and hinted[1])
                            collected.section_label = label
                            collected.section_id += 1
                    collected.section_header = (found.roles, lines[start].page)
                    collected.holdings_columns, collected.activity_parts = None, found.parts
                    collected.activity_header = True
                    collected.tables += 1
                    collected.section_tables += 1
                    collected.last_rows = {}
                continue
            index += 1
            if self._is_heading(line, self.ignored_tables):
                collected.ignoring = True
                collected.holdings_columns = collected.activity_parts = None
                continue
            if collected.ignoring:
                if not self._is_heading(line, self.section_headings):
                    continue
                collected.ignoring = False
            if (
                collected.section is not None
                and not collected.section_tables
                and len(line.cells) <= 2
                and len(collected.sub_headings) < _MAX_SUB_HEADINGS
                and not find_amounts(line.text)
            ):
                collected.sub_headings.append(line.norm)
            self._line(line, collected)

    def _line(self, line: ViewLine, collected: _Collected) -> None:
        norm = line.norm
        if any(norm.startswith(prefix.strip()) for prefix in self.skip_prefixes):
            return
        holdings, parts = collected.holdings_columns, collected.activity_parts
        if (holdings is not None or parts is not None) and _header_like(line):
            # A table header we cannot read: the table above ends here.
            collected.holdings_columns = collected.activity_parts = None
            return
        if holdings is not None:
            group = self._group(norm)
            if group is not None and not find_amounts(line.text):
                collected.group = group
                collected.last_holding = None
                return
            if norm.startswith("total"):
                self._holdings_total(line, collected)
                collected.last_holding = None
                return
            cells = _assign(line, holdings)
            name_x0 = next((c.x0 for c in holdings if c.role == "name"), None)
            row = self._holding(line, cells, collected.group, name_x0)
            if row is not None:
                if collected.group is not None and collected.group in self.cash_groups:
                    collected.cash.append(row)
                else:
                    collected.holdings.append(row)
                collected.last_holding = row
                return
            last = collected.last_holding
            if last is not None and self._continue_holding(line, cells, last):
                return
            group = self._group(norm, prefix=True)
            if group is not None and len(line.cells) == 1 and not find_amounts(line.text):
                # "Exchange Traded Products E (e.g. ETF, ETN)".
                collected.group = group
                collected.last_holding = None
                return
            collected.last_holding = None
        elif parts is not None:
            if collected.section == "skip":
                return  # pending trades, daily balances: no activity
            before = len(collected.activity) + collected.skipped
            for number, part in enumerate(parts):
                piece = _piece(line, part) if len(parts) > 1 else line
                if piece is None:
                    continue
                if (
                    piece.norm.startswith(("total", "subtotal"))
                    or piece.norm.split(" ", 1)[0] in _TOTAL_WORDS
                ):
                    self._section_total(piece, part, collected)
                    continue
                self._activity(
                    piece,
                    _assign(piece, part.columns),
                    collected,
                    symbol_column=any(c.role == "symbol" for c in part.columns),
                    part=number,
                    column_suggestion=part.suggested,
                )
            if len(collected.activity) + collected.skipped != before:
                return
            if norm.startswith("total"):
                return
        elif (
            collected.section != "skip"
            and len(line.cells) >= 2
            and _date_shaped(line.cells[0].text)
            and find_amounts(line.text)
        ):
            # A date and an amount under no table that could be read: an entry
            # of a table whose header the parser does not know.
            _region(collected.unread, line)
        self._figure(line, collected)
        self._scan_summary(line, collected.summary)

    # -- dates ----------------------------------------------------------------------

    def _styles(
        self, rows: Sequence[_ActivityRow], hint: str | None
    ) -> tuple[tuple[DateStyle, ...], bool]:
        if self.date_styles:
            return self.date_styles, True
        numeric = [r.date_text for r in rows if numeric_parts(r.date_text) is not None]
        if not numeric:
            return ("d_mon_y", "mon_d_y", "ymd", "mdy"), True
        style, certain = guess_numeric_style(numeric)
        if hint in NUMERIC_DM and (not certain or style == hint):
            style, certain = hint, True
        chosen: DateStyle = style if style is not None else "mdy"
        # ``MM/DD`` rows take their year from the period (read_date's window).
        yearless: DateStyle = "md" if chosen == "mdy" else "dm"
        return (chosen, yearless, "ymd", "d_mon_y", "mon_d_y"), certain

    def _period(
        self,
        summary: _Summary,
        styles: tuple[DateStyle, ...],
        view: DocumentView,
        certain: bool,
    ) -> PeriodRead | None:
        ordered: list[DateStyle] = [s for s in self.period_styles if s in FULL_STYLES]
        for style in styles:
            numeric = full_style(style)
            if numeric in NUMERIC_DM and numeric in ordered:
                ordered.remove(numeric)
                ordered.insert(0, numeric)
        return read_period(summary.period.texts, ordered, view, certain=certain)

    @staticmethod
    def _read_date(
        text: str, styles: tuple[DateStyle, ...], window: tuple[date, date] | None
    ) -> date | None:
        for style in styles:
            found = read_date(text, style, window=window)
            if found is not None:
                return found
        return None

    # -- the statement --------------------------------------------------------------

    def _security(
        self,
        symbol: str | None,
        name: str | None,
        known: Mapping[str, str | None],
        *,
        cusip: str | None = None,
        isin: str | None = None,
        asset_class: AssetClassCode | None = None,
    ) -> SecurityRef | None:
        """The row's security: by its identifiers, else by its name alone."""
        clean_name = (name or (known.get(symbol) if symbol else None) or "").strip()[:200] or None
        if symbol is None and cusip is None and isin is None and not has_word(clean_name):
            return None
        try:
            return SecurityRef(
                symbol=symbol,
                cusip=cusip,
                isin=isin,
                name=clean_name,
                asset_class=asset_class or guess_asset_class(clean_name),
            )
        except ValidationError:
            return None

    def _positions(
        self, collected: _Collected, as_of: date | None
    ) -> tuple[list[InvestmentPosition], list[dict[str, float]]]:
        positions: list[InvestmentPosition] = []
        scores: list[dict[str, float]] = []
        for row in collected.holdings:
            symbol, name = row.symbol, row.name
            if symbol is None and row.cusip is None and row.isin is None:
                # Nothing else identifies it: a ticker in front of its name,
                # else the name alone.
                symbol, name = _leading_ticker(row)
            security = self._security(
                symbol,
                name,
                {},
                cusip=row.cusip,
                isin=row.isin,
                asset_class=ASSET_GROUPS.get(row.group) if row.group else None,
            )
            if security is None:
                collected.skipped += 1
                continue
            score = {
                "security": conf.scaled(self.base, row.ocr),
                "quantity": conf.scaled(self.base, row.ocr),
                "market_value": conf.scaled(
                    self.base if row.value is not None else conf.MISSING, row.ocr
                ),
            }
            if row.price is not None:
                score["unit_price"] = conf.scaled(self.base, row.ocr)
            positions.append(
                InvestmentPosition(
                    security=security,
                    quantity=row.quantity,
                    unit_price=row.price,
                    price_per_100=_per_100(row),
                    market_value=row.value.value if row.value is not None else None,
                    cost_basis=abs(row.cost.value) if row.cost is not None else None,
                    price_as_of=as_of,
                    page=row.page,
                    bbox=row.bbox,
                )
            )
            scores.append(score)
        return positions, scores

    def _activities(
        self,
        collected: _Collected,
        styles: tuple[DateStyle, ...],
        certain: bool,
        period: tuple[date, date] | None,
        known: Mapping[str, str | None],
        held: Mapping[str, SecurityRef] | None = None,
        per_100: Sequence[InvestmentPosition] = (),
    ) -> tuple[list[InvestmentActivity], list[dict[str, float]], list[_ActivityRow]]:
        """The activity rows, their scores and the collected row each was read from."""
        held = held or {}
        bonds = {k for p in per_100 for k in _keys(p.security)}
        rows: list[InvestmentActivity] = []
        scores: list[dict[str, float]] = []
        kept: list[_ActivityRow] = []
        # A table that prints a minus sign prints every sign: its unsigned amounts are inflows.
        signed = {r.table for r in collected.activity if r.amount.negative_marked}
        for row in collected.activity:
            trade = self._read_date(row.date_text, styles, period)
            placed = trade is not None
            if trade is None and period is not None:
                # A yearless date too far outside the period to be placed in
                # it: the nearest year, kept as a guess a person looks at
                # (the row is on the page; dropping it would lose it).
                trade = _nearest_date(row.date_text, styles, period)
            if trade is None:
                collected.skipped += 1
                collected.skipped_regions.append((row.page, row.bbox))
                continue
            settle = self._read_date(row.settle_text, styles, period) if row.settle_text else None
            symbol, name = row.symbol, row.description or None
            if symbol is None and row.cusip is None and name is not None:
                lead, rest = _split_symbol(name)
                # Beside a symbol column the name's first word is the issuer's
                # ("EXAMPLE RESORTS INTERNATIONAL", a bond with no ticker), never a
                # ticker, unless the account holds that symbol.
                guess = row.type in SECURITY_TYPES and not (row.symbol_column and row.security_name)
                if lead is not None and (lead in known or guess):
                    symbol, name = lead, rest
            security = (
                self._security(symbol, name, known, cusip=row.cusip)
                if symbol is not None or row.cusip is not None
                else None
            )
            if security is None and name is not None and row.type not in _CASH_TYPES:
                # A description that is a held security's name names it (also a
                # holding printed by its name alone).
                security = held.get(_name_key(name))
            if security is None and row.security_name and row.type in SECURITY_TYPES:
                # A trade or transfer whose security-name column is all that names
                # its security (one no longer held): that name.
                security = self._security(None, row.security_name, known)
            amount = row.amount.value
            kind = row.type
            if row.by_sign and (row.amount.negative_marked or amount < 0):
                kind, amount = "withdrawal", -abs(amount)
            elif row.by_sign and (row.table in signed or row.amount.positive_marked):
                kind = "deposit"
            elif (
                not row.amount.negative_marked
                and kind in OUTFLOW_TYPES
                and (row.by_sign or row.table not in signed)
            ):
                amount = -abs(amount)
            quantity = abs(row.quantity) if row.quantity is not None else None
            ratio = split_ratio(row.description) if row.type == "split" else None
            if row.type == "split":
                quantity = None
            date_conf = self.base if certain else conf.GUESSED
            if period is not None and not (period[0] <= trade <= period[1]):
                date_conf = min(date_conf, conf.INFERRED)
            if not placed:
                date_conf = min(date_conf, conf.GUESSED)
            score = {
                "trade_date": conf.scaled(date_conf, row.ocr),
                "type": conf.scaled(self.base, row.ocr),
                "amount": conf.scaled(self.base, row.ocr),
            }
            if row.type in SECURITY_TYPES:
                score["security"] = conf.scaled(
                    self.base if security is not None else conf.MISSING, row.ocr
                )
            if row.type == "split":
                score["ratio"] = conf.scaled(
                    self.base if ratio is not None else conf.MISSING, row.ocr
                )
            if row.suggested:
                # Never written on the model's word: a person confirms it.
                score[SUGGESTED_DOUBT] = conf.scaled(conf.SUGGESTED, row.ocr)
            if row.type in QUANTITY_TYPES:
                score["quantity"] = conf.scaled(
                    self.base if quantity is not None and quantity > 0 else conf.MISSING, row.ocr
                )
            fees = abs(row.fees.value) if row.fees is not None else Decimal(0)
            rows.append(
                InvestmentActivity(
                    type=kind,
                    trade_date=trade,
                    settle_date=settle if settle is not None and settle >= trade else None,
                    security=security,
                    quantity=quantity if quantity is None or quantity != 0 else None,
                    unit_price=abs(row.price) if row.price is not None else None,
                    price_per_100=_activity_per_100(
                        row, security is not None and bool(_keys(security) & bonds)
                    ),
                    split_ratio=ratio,
                    amount=amount,
                    fees=fees,
                    memo=_memo(row),
                    page=row.page,
                    bbox=row.bbox,
                    section=row.section_label,
                    suggestion=row.suggestion if row.suggested else None,
                )
            )
            scores.append(score)
            kept.append(row)
        return rows, scores, kept

    @staticmethod
    def _sweep_as_cash(collected: _Collected) -> None:
        """A cash sweep printed as a holding is the account's cash, when the figures say so.

        Without a cash group of the layout (``cash_groups``), a holding of
        cash (a sweep or money market fund by its group or name) whose value
        is the closing cash the statement prints is that cash, not a
        position, when the positions add up to the account value only
        without it.
        """
        summary = collected.summary
        if collected.cash or summary.cash_closing is None or summary.total_value is None:
            return
        rows = collected.holdings
        if any(r.value is None for r in rows):
            return
        total = sum_amounts(r.value.value for r in rows if r.value is not None)
        closing, reported = summary.cash_closing.value, summary.total_value.value
        if abs(total + closing - reported) <= TOLERANCE:
            return
        for row in rows:
            if row.value is None or abs(row.value.value - closing) > TOLERANCE:
                continue
            group_class = ASSET_GROUPS.get(row.group) if row.group else None
            if "cash_equivalent" not in (group_class, guess_asset_class(row.name)):
                continue
            if abs(total - row.value.value + closing - reported) <= TOLERANCE:
                rows.remove(row)
                collected.cash.append(row)
                return

    @staticmethod
    def _cash_from_groups(collected: _Collected) -> tuple[Amount | None, Amount | None, Decimal]:
        """``(opening, closing, closing total)`` from the cash group rows."""
        if not collected.cash:
            return None, None, Decimal(0)
        ends = [r.value.value for r in collected.cash if r.value is not None]
        begins = [r.begin.value for r in collected.cash if r.begin is not None]
        closing = sum_amounts(ends) if len(ends) == len(collected.cash) else None
        opening = sum_amounts(begins) if len(begins) == len(collected.cash) else None
        return (
            Amount(value=opening) if opening is not None else None,
            Amount(value=closing) if closing is not None else None,
            closing if closing is not None else Decimal(0),
        )

    def _statement(
        self,
        view: DocumentView,
        collected: _Collected,
        fallback: _Summary | None,
        threshold: float,
        date_order: str | None,
    ) -> tuple[InvestmentStatement, bool]:
        """One account's statement and whether its dates were read with certainty."""
        summary = collected.summary
        self._sweep_as_cash(collected)
        if fallback is not None:
            for slot in ("as_of_text", "currency"):
                if getattr(summary, slot) is None:
                    setattr(summary, slot, getattr(fallback, slot))
            # The account's own labelled lines first, then the document's.
            summary.period = PeriodCandidates([*summary.period.texts, *fallback.period.texts])
        styles, certain = self._styles(collected.activity, date_order)
        # The day/month order is known when pinned or proven by numeric rows.
        order_known = bool(self.date_styles) or (
            certain and any(numeric_parts(r.date_text) is not None for r in collected.activity)
        )
        read = self._period(summary, styles, view, order_known)
        if read is not None and not order_known and read.order is not None:
            # The rows fit both orders (or have none); the printed period proves one.
            styles, certain = follow_order(styles, read.order), True
        period = read.span if read is not None else None
        as_of: date | None = None
        if summary.as_of_text is not None:
            found = find_dates(summary.as_of_text, styles)
            as_of = found[0] if found else None
        statement_conf: dict[str, float] = {}
        if as_of is None and period is not None:
            as_of = period[1]
        positions, position_scores = self._positions(collected, as_of)
        known = {p.security.symbol: p.security.name for p in positions if p.security.symbol}
        activity, activity_scores, activity_rows = self._activities(
            collected,
            styles,
            certain,
            period,
            known,
            _held_names(positions),
            [p for p in positions if p.price_per_100],
        )
        if read is not None:
            statement_conf["period"] = read.confidence(self.base)
        elif activity:
            period = (min(a.trade_date for a in activity), max(a.trade_date for a in activity))
            statement_conf["period"] = min(
                [conf.INFERRED, *(s["trade_date"] for s in activity_scores)]
            )
        currency = summary.currency or self.default_currency
        cash_opening, cash_closing = _dated_cash(summary, styles, period)
        positions_value = summary.positions_value
        group_open, group_close, group_total = self._cash_from_groups(collected)
        if collected.cash:
            if cash_opening is None:
                cash_opening = group_open
            if cash_closing is None:
                cash_closing = group_close
            if positions_value is not None:
                # The printed holdings total includes the cash group rows.
                positions_value = replace(
                    positions_value, value=positions_value.value - group_total
                )
        statement_conf["account_number"] = self.base if summary.last4 else conf.MISSING
        statement_conf["currency"] = self.base if currency else conf.MISSING
        # The document's account index prints each account's ending value: it
        # is checked against the account's own, and stands in for it when
        # the account's pages print none.
        indexed = collected.info.ending_value if collected.info is not None else None
        from_index = summary.total_value is None and indexed is not None
        if from_index and indexed is not None:
            summary.total_value = Amount(value=indexed)
        values = {
            "cash_opening": cash_opening,
            "cash_closing": cash_closing,
            "positions_value": positions_value,
            "total_value": summary.total_value,
        }
        for slot, value in values.items():
            statement_conf[slot] = self.base if value is not None else conf.MISSING
        if from_index:
            statement_conf["total_value"] = min(self.base, conf.DERIVED)
            indexed = None  # nothing of the account's own to compare it with

        def balance(value: Amount | None, on: date | None) -> Balance | None:
            return Balance(amount=value.value, as_of=on) if value is not None else None

        statement = InvestmentStatement(
            source_format=view.source_format,
            account=self._account(summary.last4, currency, collected.info),
            period_start=period[0] if period else None,
            period_end=period[1] if period else None,
            as_of=as_of,
            positions=positions,
            activity=activity,
            cash_opening=balance(cash_opening, period[0] if period else None),
            cash_closing=balance(cash_closing, as_of),
            positions_value=balance(positions_value, as_of),
            total_value=balance(summary.total_value, as_of),
            truncated=view.truncated,
            figures=collected.figures,
            first_page=collected.account.first_page if collected.account else None,
            last_page=collected.account.last_page if collected.account else None,
        )
        check = localise(
            statement,
            reconcile_investment(statement, indexed),
            section_checks(activity, activity_rows, collected.totals),
        )
        for index in check.position_mismatches:
            position_scores[index]["market_value"] = conf.CONTRADICTED
        cap = {"mismatch": conf.UNRECONCILED, "unavailable": conf.UNCHECKED}.get(check.status)
        if cap is not None:
            # Only the rows no figure of the statement vouches for carry the
            # doubt (:func:`localise`); a statement nothing can be checked
            # against doubts them all.
            doubted_positions = (
                range(len(position_scores))
                if check.doubtful_positions is None
                else check.doubtful_positions
            )
            doubted_activity = (
                range(len(activity_scores))
                if check.doubtful_activity is None
                else check.doubtful_activity
            )
            for index in doubted_positions:
                position_scores[index]["reconciliation"] = cap
            for index in doubted_activity:
                activity_scores[index]["reconciliation"] = cap
        statement_conf["reconciliation"] = cap if cap is not None else 1.0
        statement = statement.model_copy(
            update={
                "positions": [
                    p.model_copy(
                        update={
                            "confidence": min(s.values()),
                            "low_confidence": conf.low_fields(s, threshold),
                        }
                    )
                    for p, s in zip(positions, position_scores, strict=True)
                ],
                "activity": [
                    a.model_copy(
                        update={
                            "confidence": min(s.values()),
                            "low_confidence": conf.low_fields(s, threshold),
                        }
                    )
                    for a, s in zip(activity, activity_scores, strict=True)
                ],
                "parser": ParserRef(
                    name=self.name,
                    version=self.version,
                    institution=self.institution,
                    kind="statement",
                ),
                "confidence": {k: round(v, 4) for k, v in statement_conf.items()},
                "reconciliation": check,
            }
        )
        return statement, certain

    def _account(
        self, last4: str | None, currency: str | None, info: AccountInfo | None
    ) -> StatementAccount:
        """The statement's account: an investment type the document names, else the parser's."""
        kind = self.account_type
        if info is not None and info.account_type in INVESTMENT_KINDS:
            kind = info.account_type
        return StatementAccount(
            account_type=kind,
            last4=last4,
            currency=currency,
            name=info.name if info is not None else None,
            institution=self.display_name,
            subtype=info.subtype if info is not None and info.account_type == kind else None,
        )

    def _bank(
        self,
        view: DocumentView,
        collected: _Collected,
        preamble: _Collected | None,
        outcome: ParseOutcome,
        warnings: list[str],
        *,
        threshold: float,
        date_order: str | None,
    ) -> Statement | None:
        """An account without a brokerage table read as a bank statement (class doc)."""
        account = collected.account
        if account is None:
            return None
        read = self.bank_parser().read_statement(
            view,
            account.lines,
            threshold=threshold,
            date_order=date_order,
            period_texts=preamble.summary.period.texts if preamble is not None else (),
            account=account,
        )
        if read.statement is None or not read.statement.transactions:
            return None
        outcome.pending_items += read.pending_items
        outcome.skipped_lines += read.skipped_lines
        outcome.skipped_regions.extend(read.skipped_regions)
        warnings.extend(read.warnings)
        institution = self.display_name or read.statement.account.institution
        return read.statement.model_copy(
            update={
                "account": read.statement.account.model_copy(
                    update={
                        "institution": institution,
                        "currency": read.statement.account.currency or self.default_currency,
                    }
                )
            }
        )

    def parse(
        self, view: DocumentView, *, threshold: float, date_order: str | None = None
    ) -> ParseOutcome:
        return self.parse_with(view, threshold=threshold, date_order=date_order)

    def parse_with(
        self,
        view: DocumentView,
        *,
        threshold: float,
        date_order: str | None = None,
        vocabulary: Vocabulary | None = None,
    ) -> ParseOutcome:
        """Read the pages; ``vocabulary``: the local model's suggested words
        (:mod:`app.ingestion.parsers.vocabulary`), for rows nothing else types."""
        accounts, preamble, dropped, absent = self._sections(view, vocabulary)
        outcome = ParseOutcome()
        if not any(c.found_table for c in accounts):
            outcome.warnings.append("no_table_header")
            return outcome
        warnings: list[str] = []
        skipped = 0
        unknown = 0
        guessed = False
        for collected in accounts:
            if not collected.found_table:
                bank = self._bank(
                    view,
                    collected,
                    preamble,
                    outcome,
                    warnings,
                    threshold=threshold,
                    date_order=date_order,
                )
                if bank is not None:
                    outcome.statements.append(bank)
                else:
                    warnings.append("account_without_tables")
                continue
            statement, certain = self._statement(
                view,
                collected,
                preamble.summary if preamble is not None else None,
                threshold,
                date_order,
            )
            outcome.investments.append(statement)
            skipped += collected.skipped
            unknown += collected.unknown_activity
            guessed = guessed or not certain
        if preamble is not None:
            outside = len(preamble.holdings) + len(preamble.activity) + len(preamble.cash)
            skipped += outside + preamble.skipped
            unknown += preamble.unknown_activity
            if outside:
                warnings.append("rows_outside_account")
        unread = 0
        for collected in (*accounts, *([preamble] if preamble is not None else [])):
            for key in collected.unknown_actions:
                _remember(outcome.unknown_actions, key)
            for key in collected.unknown_headings:
                _remember(outcome.unknown_headings, key)
            for key in collected.unknown_columns:
                if (
                    key not in outcome.unknown_columns
                    and len(outcome.unknown_columns) < MAX_PHRASES
                ):
                    outcome.unknown_columns.append(key)
            outcome.skipped_regions.extend(collected.skipped_regions)
            outcome.skipped_regions.extend(collected.unread)
            unread += len(collected.unread)
        # An account the index lists and no statement was read for: its pages
        # were not found, or held no table (``account_without_tables``).
        read = {s.account.last4 for s in outcome.investments} | {
            s.account.last4 for s in outcome.statements
        }
        outcome.unread_accounts = [
            last4
            for last4 in dict.fromkeys(
                [*absent, *(c.summary.last4 for c in accounts if c.summary.last4)]
            )
            if last4 not in read and (last4 in absent or len(accounts) > 1)
        ][:MAX_STATEMENTS]
        if outcome.unread_accounts:
            warnings.append("accounts_not_read")
        if unread:
            # Entries of a table no header named: left out, counted, shown.
            outcome.skipped_lines += unread
            outcome.warnings.append("lines_unread")
        if dropped:
            warnings.append("too_many_accounts")
        outcome.skipped_lines += skipped
        if unknown:
            outcome.warnings.append("activity_type_unknown")
        if view.truncated:
            outcome.warnings.append("truncated")
        if guessed:
            outcome.warnings.append("date_order_guessed")
        outcome.warnings = list(dict.fromkeys([*outcome.warnings, *warnings]))
        return outcome


#: ``CUSIP: 94136RAC4`` / ``ISIN: US0378331005`` printed under a holding.
_IDENT_LINE: Final = re.compile(r"(CUSIP|ISIN)\s?[:#]?\s?([0-9A-Za-z*@#]{9,12})", re.IGNORECASE)
_ISIN: Final = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")
#: A line starting with one of these under a holding is a note (``— 7-day yield: 0.01%``).
_NOTE_MARKS: Final = ("\u2014", "\u2013", "-", "\u2022", "*")
#: A footnote marker printed in a numeric cell (``147.85A``, ``$8,412.36 c``).
_MARKED: Final = re.compile(
    r"(?P<value>[-+\u2212(]?[$\u00a3\u20ac]?[-+\u2212]?\d[\d,]*(?:\.\d+)?\)?)\s?(?P<mark>[A-Za-z]{1,2})"
)


def _unmarked(text: str) -> str:
    """A numeric cell without its footnote marker (``147.85A`` -> ``147.85``); CR/DR kept."""
    match = _MARKED.fullmatch(text.strip())
    if match is None or match["mark"].upper() in ("CR", "DR"):
        return text
    return match["value"]


def _sign(value: Decimal | None) -> int:
    if value is None or value == 0:
        return 0
    return 1 if value > 0 else -1


#: A margin marker ends within this of the description column's left edge.
_MARK_SLACK: Final = 1.0
#: A label this far right of its group's first label is a breakdown of the line above.
_INDENT: Final = 4.0
_MAX_SUB_HEADINGS: Final = 6
_DASHES: Final = frozenset({"-", "\u2013", "\u2014", "\u2212"})
_DATE_RANGE: Final = re.compile(
    r"\s*(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s*[-\u2013\u2014]\s*(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s*"
)
_CONTINUED_TAIL: Final = re.compile(r"\s*\((?:continued|cont'?d)\)\s*$", re.IGNORECASE)
_LABEL_MARK: Final = re.compile(r"(?:\s*\*+|\s+[A-Z])$")


def _range_end(text: str) -> str:
    """The last day of ``7/1-7/31`` (a period standing where a row's date does)."""
    match = _DATE_RANGE.fullmatch(text[:MAX_TOKEN])
    return match[2] if match is not None else text


def _heading_label(text: str) -> str:
    """A heading as printed, without ``(continued)``."""
    return _CONTINUED_TAIL.sub("", " ".join(text.split()))[:SECTION_LABEL_MAX]


def _alike(norm: str, others: Sequence[str]) -> bool:
    """``norm`` is worded like one of ``others`` (half their words, digits aside)."""

    def words(text: str) -> set[str]:
        return {w for w in text.split() if not w.isdigit()}

    mine = words(norm)
    for other in others:
        theirs = words(other)
        union = mine | theirs
        if union and 2 * len(mine & theirs) >= len(union):
            return True
    return False


def _without_mark(cells: list[Cell] | None, column_x0: float | None) -> list[Cell] | None:
    """The name cells without a lone marker letter printed in the margin before them.

    ``M EXAMPLE CORP ...``: a position held in margin is marked by one
    capital letter left of the description column (it ends where the
    column's header starts, ``column_x0``), the name starting in the column.
    """
    if not cells or column_x0 is None:
        return cells
    words = cells[0].word_cells()
    if len(words) < 2 or len(words[0].text) != 1 or not words[0].text.isupper():
        return cells
    if words[0].x1 > column_x0 + _MARK_SLACK or words[1].x0 < column_x0 - 2 * _MARK_SLACK:
        return cells
    return [join_cells(words[1:]), *cells[1:]]


def _labelled(line: ViewLine) -> tuple[str, list[Amount | None]] | None:
    """``(label, values)`` of a summary line: its words, then its figures.

    A dash stands for no figure (None); the line needs a label with a letter
    and at least one value cell. Whatever follows the figures (another
    block's label and figure printed beside them) is not read.
    """
    label: list[str] = []
    values: list[Amount | None] = []
    done = False
    for cell in line.cells:
        if done:
            break
        for word in (cell,) if read_amount(_unmarked(cell.text)) else cell.word_cells():
            text = _unmarked(word.text).strip()
            if text in _DASHES:
                values.append(None)
                continue
            amount = read_amount(text) if not any(ch.isalpha() for ch in text) else None
            if amount is not None and (label or values):
                values.append(amount)
            elif values:
                done = True
                break
            else:
                label.append(word.text)
    text = _LABEL_MARK.sub("", " ".join(label)).strip()
    if not values or not any(ch.isalpha() for ch in text):
        return None
    return text, values


def _index_of(norm: str, phrases: Sequence[str]) -> int | None:
    """Where the first of ``phrases`` starts in ``norm`` (whole words), or None."""
    padded = f" {norm} "
    found = [padded.find(f" {p} ") for p in phrases]
    hits = [i for i in found if i >= 0]
    return min(hits) if hits else None


def _piece(line: ViewLine, part: _Part) -> ViewLine | None:
    """The cells of ``line`` in one side-by-side table's span (a cell across the
    edge split into its words), or None when there are none."""
    cells: list[Cell] = []
    for cell in line.cells:
        if cell.x1 <= part.lo or cell.x0 >= part.hi:
            continue
        if part.lo <= cell.x0 and cell.x1 <= part.hi:
            cells.append(cell)
            continue
        words = [w for w in cell.word_cells() if part.lo <= w.centre < part.hi]
        if words:
            cells.append(join_cells(words))
    if not cells:
        return None
    return ViewLine(
        page=line.page,
        index=line.index,
        cells=tuple(cells),
        text=" ".join(c.text for c in cells),
        height=line.height,
    )


def _per_100(row: _HoldingRow) -> bool:
    """The row's price is quoted per 100 of face value (a bond).

    Only when the printed figures say so: quantity x price / 100 is the
    market value and quantity x price is not.
    """
    if row.price is None or row.value is None or row.price == 0:
        return False
    value = row.value.value
    return not _position_agrees(row.quantity, row.price, value, 1) and _position_agrees(
        row.quantity, row.price, value, 100
    )


#: Activity types that name no security (a held name in their memo is a payee's).
_CASH_TYPES: Final = frozenset({"deposit", "withdrawal"})


#: Words a title in title case leaves in lower case (``Portfolio of the ...``).
_TITLE_MINOR_WORDS: Final = frozenset(
    {"and", "the", "for", "from", "with", "into", "per", "via", "des", "del", "der", "von", "van"}
)


def _sentence_under_capitals(name: str | None, line: str) -> bool:
    """``line`` is a sentence under a name printed in capitals: a note on the
    holding (``Valued from an independent estimate``), not the rest of its name.

    A sentence has a word after its first written in lower case that a title
    would capitalise (``independent``, ``estimate``); a line in title case or
    capitals (``Core Total Market ETF``, ``Class R6``, ``2031 (Example
    Funds)``) is the rest of the name, and so is one ending in ``(SYMBOL)``.
    """
    letters = [ch for ch in name or "" if ch.isalpha()]
    if not letters or any(ch.islower() for ch in letters):
        return False
    if _PAREN_SYMBOL.search(line) is not None:
        return False
    words = [w.strip(".,;:()\"'") for w in line.split()[1:]]
    return any(
        len(w) >= 3 and w.isalpha() and w.islower() and w not in _TITLE_MINOR_WORDS for w in words
    )


def _name_key(name: str) -> str:
    """A security name compared without its ``(SYMBOL)``, case or punctuation."""
    return normalise(_PAREN_SYMBOL.sub(" ", name))


def _held_names(positions: Sequence[InvestmentPosition]) -> dict[str, SecurityRef]:
    """The positions' securities by name (a name two securities share names neither)."""
    found: dict[str, SecurityRef] = {}
    shared: set[str] = set()
    for position in positions:
        if not position.security.name:
            continue
        key = _name_key(position.security.name)
        if not key:
            continue
        if key in found and found[key] != position.security:
            shared.add(key)
        found.setdefault(key, position.security)
    return {k: v for k, v in found.items() if k not in shared}


#: Dated cash balance lines kept per account (bounded work).
_MAX_DATED_CASH: Final = 20


def _dated_cash(
    summary: _Summary, styles: Sequence[DateStyle], period: tuple[date, date] | None
) -> tuple[Amount | None, Amount | None]:
    """``(opening, closing)`` cash: the labelled figures, else the dated cash lines'.

    Of two or more dated lines the earliest is the opening and the latest
    the closing cash; one alone is the closing cash, or the opening cash
    when it is dated the period's first day.
    """
    opening, closing = summary.cash_opening, summary.cash_closing
    dated: list[tuple[date, Amount]] = []
    for text, amount in summary.cash_dated:
        found = find_dates(text, (*styles, *FULL_STYLES))
        if found:
            dated.append((found[0], amount))
    if not dated:
        return opening, closing
    dated.sort(key=lambda d: d[0])
    first, last = dated[0], dated[-1]
    if first[0] != last[0]:
        return (
            first[1] if opening is None else opening,
            last[1] if closing is None else closing,
        )
    if period is not None and first[0] == period[0] != period[1]:
        return (first[1] if opening is None else opening), closing
    return opening, (last[1] if closing is None else closing)


def _keys(security: SecurityRef) -> set[str]:
    """A security's identifiers (any one of them names it)."""
    return {k for k in (security.symbol, security.cusip, security.isin) if k}


def _activity_per_100(row: _ActivityRow, held_per_100: bool) -> bool:
    """An activity row's price is quoted per 100 of face value (a bond trade).

    By its figures when they tell (quantity x price / 100 is within a tenth
    of the amount and quantity x price is not: fees and accrued interest are
    in the amount), else as the statement prices the security's position.
    """
    if row.price is None or row.price == 0:
        return False
    value = abs(row.amount.value)
    if row.quantity is None or row.quantity == 0 or value == 0:
        return held_per_100
    gross = abs(row.quantity * row.price)
    margin = value / 10
    if abs(gross - value) <= margin:
        return False
    if abs(gross / 100 - value) <= margin:
        return True
    return held_per_100


def _date_shaped(text: str) -> bool:
    if numeric_parts(text) is not None:
        return True
    return any(read_date(text, style) is not None for style in ("d_mon_y", "mon_d_y", "ymd"))


def _identifier(text: str) -> tuple[str | None, str | None]:
    """``(symbol, cusip)`` of a symbol cell (``Symbol/CUSIP`` holds either)."""
    text = text.strip()
    if not text:
        return None, None
    if is_cusip(text):
        return None, text
    return (text, None) if _SYMBOL.fullmatch(text) else (None, None)


#: A ticker printed in front of a security's name (``ZZAB Example Index Fund``).
_LEADING_TICKER: Final = re.compile(r"[A-Z]{2,5}(?:\.[A-Z]{1,2})?")


def _leading_ticker(row: _HoldingRow) -> tuple[str | None, str | None]:
    """``(symbol, name)`` of a holding nothing else identifies: a ticker-shaped
    first word in capitals before a name that is not
    (``ZZAB Example Index Fund``).

    Never when the whole name is in capitals (``ZZQ NET LEASE INC ...``: the
    first word is the issuer's), for a bond priced per 100, nor for a name
    with parentheses (a layout that prints tickers prints them in them:
    ``ZZ Portfolio 2030 (Example Funds)`` has none).
    """
    raw = (row.raw_name or row.name or "").strip()
    head, _, rest = raw.partition(" ")
    rest = rest.strip()
    if (
        row.symbol_column
        or "(" in raw
        or _per_100(row)
        or not _LEADING_TICKER.fullmatch(head)
        or not any(ch.islower() for ch in rest)
    ):
        return None, row.name
    return head, rest


def _split_symbol(text: str) -> tuple[str | None, str | None]:
    """``("EXMPL", "Example Index Fund")`` from ``EXMPL Example Index Fund`` or ``... (EXMPL)``."""
    paren = _PAREN_SYMBOL.search(text)
    if paren is not None:
        rest = (text[: paren.start()] + text[paren.end() :]).strip()
        return paren[1], rest or None
    head, _, rest = text.strip().partition(" ")
    if _SYMBOL.fullmatch(head) and len(head) >= 2:
        return head, rest.strip() or None
    return None, text.strip() or None


class PlaceholderBrokerageParser(BrokerageStatementParser):
    """Placeholder Brokerage (fictional): ``MM/DD/YYYY``, USD, signed amounts."""

    name: ClassVar[str] = "placeholder_brokerage"
    version: ClassVar[str] = "6"
    institution: ClassVar[str | None] = "placeholder-brokerage-account"
    display_name: ClassVar[str | None] = "Placeholder Brokerage"
    fingerprints: ClassVar[tuple[str, ...]] = (
        "placeholder brokerage",
        "investment account statement",
    )
    default_currency: ClassVar[str | None] = "USD"
    date_styles: ClassVar[tuple[DateStyle, ...]] = ("mdy",)
    period_styles: ClassVar[tuple[DateStyle, ...]] = ("mdy",)
    base: ClassVar[float] = 1.0


class FidelityStatementParser(BrokerageStatementParser):
    """Fidelity Investments "Investment Report": one or several accounts per PDF.

    The layout as this parser expects it (drawn by
    ``tests/fixtures/synth_fidelity.py`` from invented accounts and
    instruments; no real statement is in the repository): page 1 names
    "Fidelity Investments" and "Investment Report" with the period
    (``October 1, 2026 - October 31, 2026``) and an overview of the accounts;
    each account's pages carry its title and number (``INDIVIDUAL - TOD``
    ... ``Account # Z12-345678``), an account summary (``Beginning Account
    Value`` ... ``Ending Account Value``), *Holdings* grouped by asset class
    (Core Account, Mutual Funds, Exchange Traded Products, Stocks) with
    ``Total <group>`` subtotals and ``Total Holdings``, its columns
    *Description, Beginning Market Value, Quantity, Price Per Unit, Ending
    Market Value, Total Cost Basis, Unrealized Gain/Loss, EAI ($) / EY (%)*
    over two lines with dates under them; and *Activity* tables (*Settlement
    Date, Security Name, Symbol/CUSIP, Description, Quantity, Price,
    Transaction Cost, Amount*; *Date, Reference, Description, Amount* for
    deposits and withdrawals) whose description is the action ("You
    Bought", "Dividend Received", "Reinvestment", "Electronic Funds Transfer
    Received", ...; :data:`ACTIVITY_TYPES`). Dates are ``MM/DD`` (the year
    from the period), ``MM/DD/YYYY`` or ``Mon D, YYYY``.

    Cash is the **core account** (a money market position): its rows are
    the account's opening and closing cash, not a position, so every trade,
    income and transfer is checked against the change in cash; the *Core
    Fund Activity* table (the core's own buys and sells settling those
    trades) is not read, it would count each of them twice.

    The pages printing two blocks side by side (*Account Summary* beside
    *Account Holdings*, *Core Account Cash Flow* beside *Income Summary*,
    *Debit Card Summary* beside *Debit Card Activity* with one table per
    card) are read block by block; the summary tables are the statement's
    figures, each activity section is checked against its ``Total ...``
    line and against the cash-flow summary's line for it, *Margin
    Activity* gives the period's margin interest, and the lone ``M``
    printed before a holding held in margin is no part of its name (the
    same layout drawn with invented figures:
    ``tests/fixtures/synth_fidelity_report.py``,
    ``tests/unit/test_statement_evidence.py``).
    """

    name: ClassVar[str] = "fidelity_investments_statement"
    version: ClassVar[str] = "7"
    institution: ClassVar[str | None] = "fidelity-investments-statement"
    display_name: ClassVar[str | None] = "Fidelity Investments"
    fingerprints: ClassVar[tuple[str, ...]] = ("fidelity investments", "investment report")
    all_fingerprints: ClassVar[bool] = True
    name_anchors: ClassVar[tuple[str, ...]] = ("fidelity brokerage services",)
    anchors: ClassVar[tuple[str, ...]] = (
        "accounts included in this report",
        "core account",
        "ending account value",
        "securities bought sold",
    )
    default_currency: ClassVar[str | None] = "USD"
    date_styles: ClassVar[tuple[DateStyle, ...]] = ("mdy", "md", "mon_d_y")
    period_styles: ClassVar[tuple[DateStyle, ...]] = ("mon_d_y", "mdy")
    base: ClassVar[float] = 1.0
    period_labels: ClassVar[tuple[str, ...]] = (
        "investment report",
        *BrokerageStatementParser.period_labels,
    )
    description_types: ClassVar[bool] = True
    cash_groups: ClassVar[tuple[str, ...]] = ("core account",)
    ignored_tables: ClassVar[tuple[str, ...]] = ("core fund activity",)
    section_headings: ClassVar[tuple[str, ...]] = (
        "holdings",
        "activity",
        "account summary",
        "securities bought",
        "dividends interest",
        "deposits",
        "withdrawals",
        "fees and charges",
        "other activity",
        "trades pending",
        "income summary",
    )


BROKERAGE_PARSERS: Final[tuple[type[BrokerageStatementParser], ...]] = (
    FidelityStatementParser,
    PlaceholderBrokerageParser,
    BrokerageStatementParser,
)
