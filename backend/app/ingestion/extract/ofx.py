# SPDX-License-Identifier: AGPL-3.0-or-later
"""OFX and QFX extraction, SGML (v1) and XML (v2) (INGESTION.md; SECURITY.md C6).

* **XML (OFX 2.x)**: parsed with ``defusedxml`` with DTDs, entity
  declarations and external references all forbidden, so XXE and
  entity-expansion bombs ("billion laughs") are refused before any expansion.
  A file that opens with ``<?xml`` or ``<?OFX`` takes this route. The bytes
  are decoded here and the XML declaration is dropped before parsing, so the
  encoding a file declares (often wrong, sometimes unknown to the parser) is
  never used.
* **SGML (OFX 1.x)**: a small tokenizer of our own (``<TAG>value`` with
  optional end tags for leaf elements). A tag with no value opens an
  aggregate only when its name has an end tag somewhere in the file or is an
  aggregate the readers walk (:data:`AGGREGATES`); otherwise it is a leaf
  printed empty, and the fields after it stay where they are. It never
  resolves entities beyond the five predefined ones and numeric references,
  and any markup declaration (``<!DOCTYPE``, ``<!ENTITY``, ``<![CDATA[``...)
  refuses the file. An OFX 2
  opening over a body that is not well-formed XML (SGML leaves with no end
  tag) is read by this tokenizer once its leading ``<?xml ...?>`` and
  ``<?OFX ...?>`` instructions are removed; nothing else is removed, so every
  other instruction or declaration still refuses the file, and a DTD refuses
  it on the XML route before any fallback. A file cut short (no ``</OFX>``)
  does not fall back.
* Both are turned into the same small tree and walked for bank
  (``STMTRS``) and credit card (``CCSTMTRS``) statements: the institution
  (``FI/ORG``, used with the last four digits to match the account), account type, the
  **last four digits** of the account id (the full number never leaves the
  sandbox), currency, period, ledger and available balances, transactions
  (``FITID``, type, posted and user dates, amount, name/payee, memo, check
  number). Transactions are the ``STMTTRN`` of the statement's
  ``BANKTRANLIST`` and those directly under the statement (a stray end tag
  closes the list early in some exports). Investment statements
  (``INVSTMTRS``: positions, activity, cash) are read by
  :mod:`app.ingestion.extract.ofx_invest` on the same tree.
* **No record is dropped without a count**: a transaction with no readable
  date or amount, or one the reader cannot reach (nested where no transaction
  belongs), is counted, and each statement that lost records adds one warning
  ``transaction_skipped:<n>`` (:func:`skipped_warnings`).
* Caps: nesting depth, element count, transactions (``max_transactions``,
  then ``truncated``). Text fields are whitespace-collapsed, cut and
  formula-neutralised (C7).
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Final

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError
from defusedxml.ElementTree import fromstring as defused_fromstring

from app.ingestion.extract.common import (
    ExtractFailure,
    clean_text,
    decode_text,
    last4,
    parse_amount,
)
from app.ingestion.extract.ofx_invest import INVESTMENT_AGGREGATES, investment_statements
from app.ingestion.extract.registry import register
from app.ingestion.sandbox.protocol import (
    MAX_STATEMENTS,
    WARNINGS_MAX,
    Balance,
    ExtractedTransaction,
    ExtractOptions,
    ExtractResult,
    Statement,
    StatementAccount,
)

if TYPE_CHECKING:
    # Type only: parsing goes through defusedxml.
    from xml.etree.ElementTree import Element  # nosec B405

VERSION: Final = "ofx/2"
MAX_DEPTH: Final = 64
MAX_ELEMENTS: Final = 2_000_000

#: Unescape passes after the first, for values an exporter escaped more than once.
MAX_EXTRA_UNESCAPES: Final = 2

#: Statement aggregates: each is read on its own, wherever it sits.
STATEMENTS: Final = frozenset({"STMTRS", "CCSTMTRS", "INVSTMTRS"})

#: Aggregates the readers walk. In SGML an aggregate always carries its end
#: tag, and that is how the tokenizer knows one; these names nest even in a
#: file that leaves the end tag out, as they did before empty leaves were told
#: apart from aggregates.
AGGREGATES: Final = INVESTMENT_AGGREGATES | {
    "OFX",
    "SIGNONMSGSRSV1",
    "SONRS",
    "STATUS",
    "FI",
    "BANKMSGSRSV1",
    "STMTTRNRS",
    "STMTRS",
    "CREDITCARDMSGSRSV1",
    "CCSTMTTRNRS",
    "CCSTMTRS",
    "BANKACCTFROM",
    "CCACCTFROM",
    "BANKTRANLIST",
    "STMTTRN",
    "PAYEE",
    "CURRENCY",
    "ORIGCURRENCY",
    "LEDGERBAL",
    "AVAILBAL",
}

_TOKEN: Final = re.compile(r"<(/?)([A-Za-z0-9_.]+)>([^<]*)")
_END_TAG: Final = re.compile(r"</([A-Za-z0-9_.]+)>")
_DECLARATION: Final = re.compile(r"<[!?]")
_STILL_ESCAPED: Final = re.compile(r"&(?:lt|gt|amp);")
#: How an OFX 2 file opens: the XML declaration, or the OFX instruction alone.
_XML_OPENING: Final = re.compile(r"<\?(?:xml|ofx)[\s?]", re.IGNORECASE)
_XML_DECLARATION: Final = re.compile(r"<\?xml\s[^<>]*\?>", re.IGNORECASE)
#: The ``<?xml ...?>`` and ``<?OFX ...?>`` instructions at the very start, and
#: only those: ``<?xml-stylesheet`` does not match, nor anything further down.
_LEADING_INSTRUCTIONS: Final = re.compile(r"(?:\s*<\?(?:xml|ofx)\s[^<>]*\?>)+", re.IGNORECASE)
_OFX_END: Final = re.compile(r"</OFX>", re.IGNORECASE)
_DATE: Final = re.compile(r"^\s*(\d{4})(\d{2})(\d{2})")
_CURRENCY: Final = re.compile(r"^[A-Z]{3}$")


@dataclass
class Node:
    name: str
    value: str | None = None
    children: list[Node] = field(default_factory=list)

    def child(self, name: str) -> Node | None:
        for node in self.children:
            if node.name == name:
                return node
        return None

    def text(self, *path: str) -> str | None:
        node: Node | None = self
        for name in path:
            node = node.child(name) if node is not None else None
        return node.value if node is not None else None

    def iter(self, name: str) -> Iterator[Node]:
        stack = [self]
        while stack:
            node = stack.pop()
            if node.name == name:
                yield node
            stack.extend(reversed(node.children))


def skipped_warnings(counts: Sequence[int], room: int) -> list[str]:
    """``transaction_skipped:<n>`` for each statement that did not read ``n`` records.

    One warning per statement, in order, and never deduplicated: the importer
    adds the counts up. ``room`` is how many warnings the result can still
    hold; with more statements than that, the last warning carries the sum of
    the rest, so the total is always the number of records not read.
    """
    counts = [count for count in counts if count > 0]
    room = max(room, 1)
    if len(counts) > room:
        counts = [*counts[: room - 1], sum(counts[room - 1 :])]
    return [f"transaction_skipped:{count}" for count in counts]


# ---------------------------------------------------------------------------
# SGML (OFX 1.x)
# ---------------------------------------------------------------------------


def _split_sgml_header(text: str) -> tuple[dict[str, str], str]:
    start = text.find("<")
    if start < 0:
        raise ExtractFailure("malformed_ofx", "no OFX body")
    headers: dict[str, str] = {}
    for line in text[:start].splitlines():
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().upper()] = value.strip().upper()
    return headers, text[start:]


def _sgml_encoding(headers: dict[str, str]) -> tuple[str, ...]:
    if headers.get("ENCODING") == "UTF-8":
        return ("utf-8-sig", "cp1252", "latin-1")
    return ("cp1252", "latin-1")


def _unescape(value: str) -> str:
    # Predefined and numeric references only; html.unescape never expands
    # anything into more than a character or two. Some exporters escape a
    # value twice (``&amp;lt;``): while the result still holds ``&lt;``,
    # ``&gt;`` or ``&amp;`` it is unescaped again, a bounded number of times.
    value = html.unescape(value)
    for _ in range(MAX_EXTRA_UNESCAPES):
        if not _STILL_ESCAPED.search(value):
            break
        value = html.unescape(value)
    return value


def parse_sgml(body: str) -> Node:
    if _DECLARATION.search(body):
        raise ExtractFailure("markup_declaration_forbidden", "markup declarations are not allowed")
    # Names closed somewhere in the file: only these, and the aggregates the
    # readers walk, nest when printed with no value. Any other empty tag is a
    # leaf with nothing in it (``<NAME>`` on a line of its own), and taking it
    # for an aggregate would put the fields that follow inside it.
    closed = {name.upper() for name in _END_TAG.findall(body)}
    root = Node("#root")
    stack = [root]
    count = 0
    for match in _TOKEN.finditer(body):
        closing, name, raw = match.group(1), match.group(2).upper(), match.group(3)
        count += 1
        if count > MAX_ELEMENTS:
            raise ExtractFailure("too_many_elements", "element limit exceeded")
        if closing:
            # Close the nearest open aggregate with this name; a stray end tag
            # (e.g. of a leaf) is ignored.
            for depth in range(len(stack) - 1, 0, -1):
                if stack[depth].name == name:
                    del stack[depth:]
                    break
            continue
        value = raw.strip()
        if value:
            stack[-1].children.append(Node(name, _unescape(value)))
        elif name not in closed and name not in AGGREGATES:
            stack[-1].children.append(Node(name))
        else:
            node = Node(name)
            stack[-1].children.append(node)
            stack.append(node)
            if len(stack) > MAX_DEPTH:
                raise ExtractFailure("too_deep", "nesting limit exceeded")
    ofx = root.child("OFX")
    if ofx is None:
        raise ExtractFailure("malformed_ofx", "no OFX element")
    return ofx


# ---------------------------------------------------------------------------
# XML (OFX 2.x)
# ---------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].upper()


def _convert(element: Element, depth: int, counter: list[int]) -> Node:
    counter[0] += 1
    if counter[0] > MAX_ELEMENTS:
        raise ExtractFailure("too_many_elements", "element limit exceeded")
    if depth > MAX_DEPTH:
        raise ExtractFailure("too_deep", "nesting limit exceeded")
    children = [_convert(child, depth + 1, counter) for child in element]
    value = (element.text or "").strip() or None
    return Node(_local(element.tag), None if children else value, children)


def parse_xml(text: str) -> Node:
    """Parse decoded OFX 2 text.

    The XML declaration is dropped first: ``text`` is already decoded, so the
    encoding the file names is never looked up or applied.
    """
    declaration = _XML_DECLARATION.match(text)
    if declaration is not None:
        text = text[declaration.end() :]
    try:
        element = defused_fromstring(
            text, forbid_dtd=True, forbid_entities=True, forbid_external=True
        )
    except DefusedXmlException:
        # Caught first on purpose: it is a ValueError and must never fall back.
        raise ExtractFailure("xml_forbidden", "DTDs and entities are not allowed") from None
    except (ParseError, LookupError, ValueError):
        raise ExtractFailure("malformed_xml", "the XML is not well-formed") from None
    root = _convert(element, 0, [0])
    if root.name != "OFX":
        raise ExtractFailure("malformed_ofx", "no OFX element")
    return root


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------


def _date(value: str | None) -> date | None:
    if not value:
        return None
    match = _DATE.match(value)
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _currency(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().upper()
    return value if _CURRENCY.match(value) else None


def _balance(node: Node | None) -> Balance | None:
    if node is None:
        return None
    amount = parse_amount(node.text("BALAMT"))
    if amount is None:
        return None
    return Balance(amount=amount, as_of=_date(node.text("DTASOF")))


def _transaction(node: Node) -> ExtractedTransaction | None:
    """The transaction of a ``STMTTRN``; ``None`` without a readable date and amount."""
    posted = _date(node.text("DTPOSTED"))
    amount = parse_amount(node.text("TRNAMT"))
    if posted is None or amount is None:
        return None
    name = node.text("NAME") or node.text("PAYEE", "NAME")
    currency = _currency(node.text("CURRENCY", "CURSYM") or node.text("ORIGCURRENCY", "CURSYM"))
    trntype = (node.text("TRNTYPE") or "").strip().lower()
    return ExtractedTransaction(
        posted=posted,
        user_date=_date(node.text("DTUSER")),
        amount=amount,
        payee=clean_text(name),
        memo=clean_text(node.text("MEMO")),
        fitid=clean_text(node.text("FITID"), limit=255),
        check_number=clean_text(node.text("CHECKNUM"), limit=64),
        type=trntype[:64] if re.fullmatch(r"[a-z0-9_.:-]+", trntype) else None,
        currency=currency,
    )


def _transaction_nodes(node: Node) -> list[Node]:
    """The ``STMTTRN`` the statement is read from, in the file's order.

    Those of its ``BANKTRANLIST`` and those directly under the statement
    itself: a stray ``</BANKTRANLIST>`` between two transactions closes the
    list early, and the rest land beside it.
    """
    found: list[Node] = []
    for child in node.children:
        if child.name == "STMTTRN":
            found.append(child)
        elif child.name == "BANKTRANLIST":
            found.extend(item for item in child.children if item.name == "STMTTRN")
    return found


def _transaction_count(node: Node) -> int:
    """Every ``STMTTRN`` under the statement at any depth (not in a nested statement)."""
    count = 0
    stack = list(node.children)
    while stack:
        item = stack.pop()
        if item.name in STATEMENTS:
            continue
        if item.name == "STMTTRN":
            count += 1
        stack.extend(item.children)
    return count


def _statement(
    node: Node,
    source_format: str,
    options: ExtractOptions,
    budget: list[int],
    warnings: set[str],
    skipped: list[int],
    institution: str | None = None,
) -> Statement:
    account_node = node.child("BANKACCTFROM") or node.child("CCACCTFROM")
    account_type = "creditcard" if node.name == "CCSTMTRS" else None
    number: str | None = None
    if account_node is not None:
        number = account_node.text("ACCTID")
        if account_type is None:
            raw_type = (account_node.text("ACCTTYPE") or "").strip().lower()
            account_type = raw_type if re.fullmatch(r"[a-z]{1,32}", raw_type) else None
    transactions: list[ExtractedTransaction] = []
    tranlist = node.child("BANKTRANLIST")
    truncated = False
    items = _transaction_nodes(node)
    # A transaction nested where the reader does not look (inside another
    # one whose end tag is missing, say) is not read: it is counted.
    unread = _transaction_count(node) - len(items)
    for item in items:
        if budget[0] <= 0:
            truncated = True
            break
        transaction = _transaction(item)
        if transaction is None:
            unread += 1
            continue
        transactions.append(transaction)
        budget[0] -= 1
    if truncated:
        warnings.add(f"transactions_truncated:{options.max_transactions}")
    skipped.append(unread)
    return Statement(
        source_format="qfx" if source_format == "qfx" else "ofx",
        account=StatementAccount(
            account_type=account_type,
            last4=last4(number),
            currency=_currency(node.text("CURDEF")),
            institution=institution,
        ),
        period_start=_date(tranlist.text("DTSTART")) if tranlist is not None else None,
        period_end=_date(tranlist.text("DTEND")) if tranlist is not None else None,
        ledger_balance=_balance(node.child("LEDGERBAL")),
        available_balance=_balance(node.child("AVAILBAL")),
        transactions=transactions,
        truncated=truncated,
    )


def _institution(root: Node) -> str | None:
    """The ``SIGNONMSGSRSV1/SONRS/FI/ORG`` name (used to tell accounts apart)."""
    for sonrs in root.iter("SONRS"):
        fi = sonrs.child("FI")
        if fi is not None:
            return clean_text(fi.text("ORG"), limit=200)
    return None


def _parse_ofx2(text: str) -> Node:
    """Text that opens with ``<?xml`` or ``<?OFX``: XML, else the SGML tokenizer.

    Only XML that is not well-formed falls back. A DTD or an entity
    declaration has already refused the file (``xml_forbidden``), and the
    tokenizer refuses every ``<?`` and ``<!`` left after the leading
    instructions. A file with no ``</OFX>`` was cut short: it stays
    ``malformed_xml``, because the tokenizer would read what is left of it
    (half an amount included) without a word.
    """
    try:
        return parse_xml(text)
    except ExtractFailure as failure:
        if failure.code != "malformed_xml" or _OFX_END.search(text) is None:
            raise
        malformed = failure
    leading = _LEADING_INSTRUCTIONS.match(text)
    try:
        return parse_sgml(text[leading.end() :] if leading is not None else text)
    except ExtractFailure as failure:
        if failure.code == "malformed_ofx":
            # Neither XML nor an SGML body with an OFX element: say the former.
            raise malformed from None
        raise


def _parse(data: bytes) -> Node:
    text, _ = decode_text(data, encodings=("utf-8-sig", "cp1252", "latin-1"))
    opening = text.lstrip("\ufeff \t\r\n")
    if _XML_OPENING.match(opening):
        return _parse_ofx2(opening)
    headers, _ = _split_sgml_header(text)
    text, _ = decode_text(data, encodings=_sgml_encoding(headers))
    _, body = _split_sgml_header(text)
    return parse_sgml(body)


def extract_ofx(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    root = _parse(data)
    warnings: set[str] = set()
    #: Records not read, per statement (a set would merge two equal counts).
    skipped: list[int] = []
    budget = [options.max_transactions]
    institution = _institution(root)
    nodes = [node for name in ("STMTRS", "CCSTMTRS") for node in root.iter(name)]
    if len(nodes) > MAX_STATEMENTS:
        warnings.add(f"statements_truncated:{MAX_STATEMENTS}")
    statements = [
        _statement(node, kind, options, budget, warnings, skipped, institution)
        for node in nodes[:MAX_STATEMENTS]
    ]
    investments = investment_statements(root, kind, options, budget, warnings, skipped, institution)
    if not statements and not investments:
        raise ExtractFailure("no_statements", "no bank, credit card or investment statement found")
    return ExtractResult(
        kind=kind,
        ok=True,
        extractor_version=VERSION,
        statements=statements,
        investments=investments,
        warnings=[*sorted(warnings), *skipped_warnings(skipped, WARNINGS_MAX - len(warnings))],
    )


register("ofx", extract_ofx)
register("qfx", extract_ofx)
