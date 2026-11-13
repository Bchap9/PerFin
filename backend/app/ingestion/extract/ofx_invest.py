# SPDX-License-Identifier: AGPL-3.0-or-later
"""OFX investment statements (``INVSTMTRS``), read in the sandbox child (INGESTION.md).

Called by :mod:`app.ingestion.extract.ofx` on the tree it already built with
the hardened SGML tokenizer or ``defusedxml`` (so nothing here parses bytes).
For every ``INVSTMTRS``:

* the account: ``INVACCTFROM/ACCTID`` cut to its **last four digits** (the
  full number never leaves the child), the broker (``FI/ORG`` or
  ``BROKERID``), ``CURDEF``; a statement that carries ``INV401K`` or
  ``INV401KBAL`` (only a 401(k) plan's does) is a ``retirement`` account of
  subtype ``401k``, any other a ``brokerage`` account (nothing inside those
  aggregates is read: the employer and the plan are not kept);
* ``INVPOSLIST``: ``POSSTOCK``/``POSMF``/``POSDEBT``/``POSOPT``/``POSOTHER``
  with ``SECID``, ``UNITS`` (negative for ``POSTYPE`` ``SHORT``),
  ``UNITPRICE``, ``MKTVAL``, ``DTPRICEASOF``; ``DTASOF`` of the list (else
  the statement's ``DTASOF``/``DTEND``) dates them;
* the two prices OFX does not quote for one unit. A bond's ``UNITPRICE`` is
  a percentage of par while its ``UNITS`` are face value, so a ``POSDEBT``
  (and a ``BUYDEBT``/``SELLDEBT``) is marked ``price_per_100``. An option's
  is the price of one share while its ``UNITS`` are contracts, so a
  ``POSOPT`` (and a ``BUYOPT``/``SELLOPT``) carries the price of one
  contract: ``UNITPRICE`` x ``SHPERCTRCT`` of the security's ``OPTINFO``
  (else the trade aggregate's own; 100 when the file has none). Brokers
  do not all follow the definition,
  so the value printed beside the price settles it: the reading whose
  product is strictly nearer to ``MKTVAL`` (a trade's ``TOTAL``) is kept,
  and the definition applies when no value is printed (:func:`_nearer`).
  No other kind of security is ever rescaled;
* ``INVTRANLIST``: buys and sells of every kind (``BUY*``/``SELL*`` with
  ``INVBUY``/``INVSELL``), ``INCOME`` (dividends, interest, capital gain
  distributions), ``REINVEST`` (reported as the income **and** a
  ``reinvest`` of the same amount, so the cash nets to zero as it does in
  the account), ``INVEXPENSE``, ``TRANSFER``, ``SPLIT`` (``split_ratio`` =
  ``(NEWUNITS, OLDUNITS)``) and ``INVBANKTRAN`` (cash ``deposit`` /
  ``withdrawal``). **No activity is dropped without a count**: one of a
  known kind that cannot be read, one of a kind this module has no reading
  for (margin interest, a return of capital, a journal entry: the warning
  ``investment_activity_kind_unsupported`` names the reason) and one of a
  known kind that sits beside the list (after a stray ``</INVTRANLIST>``)
  are each counted, and a statement that lost any adds one
  ``transaction_skipped:<n>`` to the result's warnings. A tag of the list
  with nothing inside it is no activity and is not counted;
* ``INVBAL/AVAILCASH``: the closing cash balance;
* ``SECLISTMSGSRSV1``: ticker, name and kind of each ``SECID`` (a CUSIP or
  ISIN ``UNIQUEID`` is kept as such; without a ticker the CUSIP/ISIN is the
  symbol).

Signs follow the ledger: ``amount`` is the cash effect on the account (the
``TOTAL`` OFX already signs that way), ``fees`` = ``COMMISSION`` + ``FEES``
(non-negative). Only identifiers of instruments leave the child, never the
holder's identity. Activity is capped by ``max_transactions`` (``truncated``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Sequence
from datetime import date
from decimal import Decimal
from typing import Final, Protocol

from pydantic import ValidationError

from app.ingestion.extract.common import clean_text, last4, parse_amount
from app.ingestion.sandbox.protocol import (
    MAX_POSITIONS,
    MAX_STATEMENTS,
    AssetClassCode,
    Balance,
    ExtractOptions,
    InvestmentActivity,
    InvestmentActivityType,
    InvestmentPosition,
    InvestmentStatement,
    SecurityRef,
    StatementAccount,
)

_SYMBOL: Final = re.compile(r"^[A-Za-z0-9.^/:_-]{1,32}$")
_CUSIP: Final = re.compile(r"^[0-9A-Z*@#]{9}$")
_ISIN: Final = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")

#: Position aggregates and the asset class they imply.
POSITION_KINDS: Final[dict[str, AssetClassCode]] = {
    "POSSTOCK": "equity",
    "POSMF": "mutual_fund",
    "POSDEBT": "bond",
    "POSOPT": "option",
    "POSOTHER": "other",
}
#: SECLIST info aggregates and the asset class they imply.
INFO_KINDS: Final[dict[str, AssetClassCode]] = {
    "STOCKINFO": "equity",
    "MFINFO": "mutual_fund",
    "DEBTINFO": "bond",
    "OPTINFO": "option",
    "OTHERINFO": "other",
}
_BUY: Final = ("BUYSTOCK", "BUYMF", "BUYDEBT", "BUYOPT", "BUYOTHER")
_SELL: Final = ("SELLSTOCK", "SELLMF", "SELLDEBT", "SELLOPT", "SELLOTHER")
_INCOME_TYPES: Final[dict[str, InvestmentActivityType]] = {
    "DIV": "dividend",
    "INTEREST": "interest",
    "CGLONG": "dividend",
    "CGSHORT": "dividend",
    "MISC": "dividend",
}
_KNOWN: Final = frozenset(
    {*_BUY, *_SELL, "INCOME", "REINVEST", "INVEXPENSE", "TRANSFER", "SPLIT", "INVBANKTRAN"}
)
#: Aggregates this module walks: the SGML tokenizer nests them even when a
#: file leaves their end tags out (:data:`app.ingestion.extract.ofx.AGGREGATES`).
INVESTMENT_AGGREGATES: Final = frozenset(
    {
        *_KNOWN,
        *POSITION_KINDS,
        *INFO_KINDS,
        "INVSTMTMSGSRSV1",
        "INVSTMTTRNRS",
        "INVSTMTRS",
        "INVACCTFROM",
        "INVTRANLIST",
        "INVPOSLIST",
        "INVPOS",
        "INVBAL",
        "INVTRAN",
        "INVBUY",
        "INVSELL",
        "SECID",
        "SECLISTMSGSRSV1",
        "SECLIST",
        "SECINFO",
        "STMTTRN",
    }
)
#: Aggregates of an ``INVSTMTRS`` that only a 401(k) plan's statement carries.
#: Not in :data:`INVESTMENT_AGGREGATES`: found as a child of the statement
#: whether or not the tokenizer nests them, and nothing inside them is read.
_PLAN_401K: Final = ("INV401K", "INV401KBAL")
#: Shares per option contract when the file does not say, and the largest
#: ``SHPERCTRCT`` believed.
DEFAULT_CONTRACT_SIZE: Final = Decimal(100)
MAX_CONTRACT_SIZE: Final = Decimal(10_000)
#: Trade aggregates whose security is of a known kind even without a ``SECLIST``.
_TRADE_CLASSES: Final[dict[str, AssetClassCode]] = {
    "BUYDEBT": "bond",
    "SELLDEBT": "bond",
    "BUYOPT": "option",
    "SELLOPT": "option",
}
_QUANTITY_LIMIT: Final = Decimal(10) ** 18
_DATE: Final = re.compile(r"^\s*(\d{4})(\d{2})(\d{2})")


class OfxNode(Protocol):
    """The tree node of :mod:`app.ingestion.extract.ofx` (structural)."""

    @property
    def name(self) -> str: ...

    @property
    def value(self) -> str | None: ...

    @property
    def children(self) -> Sequence[OfxNode]: ...

    def child(self, name: str) -> OfxNode | None: ...

    def text(self, *path: str) -> str | None: ...

    def iter(self, name: str) -> Iterator[OfxNode]: ...


def _date(value: str | None) -> date | None:
    match = _DATE.match(value or "")
    if match is None:
        return None
    try:
        return date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return None


def _currency(value: str | None) -> str | None:
    text = (value or "").strip().upper()
    return text if re.fullmatch(r"[A-Z]{3}", text) else None


def _quantity(raw: str | None) -> Decimal | None:
    value = parse_amount(raw)
    if value is None or abs(value) >= _QUANTITY_LIMIT:
        return None
    return value


def _nearer(printed: Decimal | None, plain: Decimal, scaled: Decimal) -> bool:
    """Whether ``printed`` is strictly nearer to ``scaled`` than to ``plain``.

    ``plain`` is units x price as the file gives them, ``scaled`` the same
    under the OFX definition of the price (module doc). ``True`` when nothing
    is printed: the definition applies.
    """
    if printed is None:
        return True
    return abs(printed - scaled) < abs(printed - plain)


def _priced(
    name: str,
    units: Decimal,
    price: Decimal | None,
    printed: Decimal | None,
    size: Decimal,
) -> tuple[Decimal | None, bool]:
    """``(unit_price, price_per_100)`` of a position or trade aggregate ``name``.

    ``printed``: the value beside the price (``MKTVAL``, ``TOTAL``); ``size``:
    the option's contract size. Only debt and option aggregates are rescaled.
    """
    if price is None:
        return None, False
    plain = abs(units * price)
    value = abs(printed) if printed is not None else None
    if name.endswith("DEBT"):
        return price, _nearer(value, plain, plain / 100)
    if name.endswith("OPT"):
        scaled = price * size
        if abs(scaled) < _QUANTITY_LIMIT and _nearer(value, plain, plain * size):
            return scaled, False
    return price, False


def _contract_size(raw: str | None) -> Decimal | None:
    """A believable ``SHPERCTRCT`` (1 to :data:`MAX_CONTRACT_SIZE`), else None."""
    size = parse_amount(raw)
    return size if size is not None and 1 <= size <= MAX_CONTRACT_SIZE else None


def _secid(node: OfxNode | None) -> tuple[str, str] | None:
    """``(UNIQUEIDTYPE, UNIQUEID)`` of a ``SECID`` aggregate, normalised."""
    if node is None:
        return None
    unique = (node.text("UNIQUEID") or "").strip().upper()
    kind = (node.text("UNIQUEIDTYPE") or "").strip().upper()
    if not unique or len(unique) > 32:
        return None
    return kind, unique


class _Catalogue:
    """``SECID`` -> :class:`SecurityRef` from the ``SECLIST`` (if the file has one)."""

    def __init__(self, root: OfxNode) -> None:
        self._infos: dict[tuple[str, str], tuple[str | None, str | None, AssetClassCode]] = {}
        self._sizes: dict[tuple[str, str], Decimal] = {}
        for info_kind, asset_class in INFO_KINDS.items():
            for info in root.iter(info_kind):
                secinfo = info.child("SECINFO")
                if secinfo is None:
                    continue
                key = _secid(secinfo.child("SECID"))
                if key is None:
                    continue
                ticker = (secinfo.text("TICKER") or "").strip().upper()
                name = clean_text(secinfo.text("SECNAME"), limit=200)
                self._infos[key] = (ticker or None, name, asset_class)
                if info_kind == "OPTINFO":
                    size = _contract_size(info.text("SHPERCTRCT"))
                    if size is not None:
                        self._sizes[key] = size

    def contract_size(self, secid: OfxNode | None, own: str | None = None) -> Decimal:
        """Shares per contract of an option: its ``OPTINFO``'s ``SHPERCTRCT``, else 100.

        ``own``: the ``SHPERCTRCT`` of the trade aggregate itself, used when
        the catalogue has none for the security. A value outside 1 to
        :data:`MAX_CONTRACT_SIZE` is not believed.
        """
        key = _secid(secid)
        listed = self._sizes.get(key) if key is not None else None
        return listed or _contract_size(own) or DEFAULT_CONTRACT_SIZE

    def ref(
        self, secid: OfxNode | None, default_class: AssetClassCode | None
    ) -> SecurityRef | None:
        key = _secid(secid)
        if key is None:
            return None
        kind, unique = key
        ticker, name, asset_class = self._infos.get(key, (None, None, default_class or "other"))
        cusip = unique if kind == "CUSIP" and _CUSIP.fullmatch(unique) else None
        isin = unique if kind == "ISIN" and _ISIN.fullmatch(unique) else None
        symbol = ticker if ticker and _SYMBOL.fullmatch(ticker) else None
        if symbol is None and cusip is None and isin is None and _SYMBOL.fullmatch(unique):
            symbol = unique  # a ticker-type or broker id; still only an instrument name
        if symbol is None and cusip is None and isin is None:
            return None
        return SecurityRef(
            symbol=symbol, cusip=cusip, isin=isin, name=name, asset_class=asset_class
        )


def _positions(
    node: OfxNode, catalogue: _Catalogue, as_of: date | None, warnings: set[str]
) -> list[InvestmentPosition]:
    out: list[InvestmentPosition] = []
    poslist = node.child("INVPOSLIST")
    if poslist is None:
        return out
    for item in poslist.children:
        default_class = POSITION_KINDS.get(item.name)
        invpos = item.child("INVPOS")
        if default_class is None or invpos is None:
            continue
        if len(out) >= MAX_POSITIONS:
            warnings.add("positions_truncated")
            break
        security = catalogue.ref(invpos.child("SECID"), default_class)
        units = _quantity(invpos.text("UNITS"))
        if security is None or units is None:
            warnings.add("position_skipped_invalid")
            continue
        if (invpos.text("POSTYPE") or "").strip().upper() == "SHORT":
            units = -abs(units)
        market_value = parse_amount(invpos.text("MKTVAL"))
        price, per_100 = _priced(
            item.name,
            units,
            _quantity(invpos.text("UNITPRICE")),
            market_value,
            catalogue.contract_size(invpos.child("SECID")),
        )
        try:
            out.append(
                InvestmentPosition(
                    security=security,
                    quantity=units,
                    unit_price=price,
                    price_per_100=per_100,
                    market_value=market_value,
                    price_as_of=_date(invpos.text("DTPRICEASOF")) or as_of,
                )
            )
        except ValidationError:
            warnings.add("position_skipped_invalid")
    return out


def _invtran(node: OfxNode) -> tuple[date | None, date | None, str | None, str | None]:
    tran = node.child("INVTRAN")
    if tran is None:
        return None, None, None, None
    return (
        _date(tran.text("DTTRADE")),
        _date(tran.text("DTSETTLE")),
        clean_text(tran.text("FITID"), limit=255),
        clean_text(tran.text("MEMO")),
    )


def _fees(node: OfxNode) -> Decimal:
    total = Decimal(0)
    for name in ("COMMISSION", "FEES", "LOAD"):
        value = parse_amount(node.text(name))
        if value is not None:
            total += abs(value)
    return total


def _trade(
    item: OfxNode, inner_name: str, kind: InvestmentActivityType, catalogue: _Catalogue
) -> list[InvestmentActivity] | None:
    inner = item.child(inner_name)
    if inner is None:
        return None
    trade_date, settle_date, fitid, memo = _invtran(inner)
    units = _quantity(inner.text("UNITS"))
    total = parse_amount(inner.text("TOTAL"))
    security = catalogue.ref(inner.child("SECID"), _TRADE_CLASSES.get(item.name))
    if trade_date is None or units is None or total is None or security is None or units == 0:
        return None
    price, per_100 = _priced(
        item.name,
        units,
        _quantity(inner.text("UNITPRICE")),
        total,
        catalogue.contract_size(inner.child("SECID"), item.text("SHPERCTRCT")),
    )
    return [
        InvestmentActivity(
            type=kind,
            trade_date=trade_date,
            settle_date=settle_date if settle_date and settle_date >= trade_date else None,
            security=security,
            quantity=abs(units),
            unit_price=price,
            price_per_100=per_100,
            # A buy takes cash out, a sale brings it in, whatever sign the file uses.
            amount=-abs(total) if kind == "buy" else abs(total),
            fees=_fees(inner),
            fitid=fitid,
            memo=memo,
        )
    ]


def _income(item: OfxNode, catalogue: _Catalogue) -> list[InvestmentActivity] | None:
    trade_date, settle_date, fitid, memo = _invtran(item)
    total = parse_amount(item.text("TOTAL"))
    if trade_date is None or total is None:
        return None
    kind = _INCOME_TYPES.get((item.text("INCOMETYPE") or "").strip().upper(), "dividend")
    return [
        InvestmentActivity(
            type=kind,
            trade_date=trade_date,
            settle_date=settle_date if settle_date and settle_date >= trade_date else None,
            security=catalogue.ref(item.child("SECID"), None),
            amount=total,
            fitid=fitid,
            memo=memo,
        )
    ]


def _reinvest(item: OfxNode, catalogue: _Catalogue) -> list[InvestmentActivity] | None:
    trade_date, _, fitid, memo = _invtran(item)
    total = parse_amount(item.text("TOTAL"))
    units = _quantity(item.text("UNITS"))
    security = catalogue.ref(item.child("SECID"), None)
    if trade_date is None or total is None or units is None or units == 0 or security is None:
        return None
    kind = _INCOME_TYPES.get((item.text("INCOMETYPE") or "").strip().upper(), "dividend")
    income_fitid = f"{fitid}:income"[:255] if fitid else None
    return [
        InvestmentActivity(
            type=kind,
            trade_date=trade_date,
            security=security,
            amount=abs(total),
            fitid=income_fitid,
            memo=memo,
        ),
        InvestmentActivity(
            type="reinvest",
            trade_date=trade_date,
            security=security,
            quantity=abs(units),
            unit_price=_quantity(item.text("UNITPRICE")),
            amount=-abs(total),
            fees=_fees(item),
            fitid=fitid,
            memo=memo,
        ),
    ]


def _expense(item: OfxNode, catalogue: _Catalogue) -> list[InvestmentActivity] | None:
    trade_date, _, fitid, memo = _invtran(item)
    total = parse_amount(item.text("TOTAL"))
    if trade_date is None or total is None:
        return None
    return [
        InvestmentActivity(
            type="fee",
            trade_date=trade_date,
            security=catalogue.ref(item.child("SECID"), None),
            amount=-abs(total),
            fees=abs(total),
            fitid=fitid,
            memo=memo,
        )
    ]


def _transfer(item: OfxNode, catalogue: _Catalogue) -> list[InvestmentActivity] | None:
    trade_date, _, fitid, memo = _invtran(item)
    units = _quantity(item.text("UNITS"))
    security = catalogue.ref(item.child("SECID"), None)
    if trade_date is None or units is None or units == 0 or security is None:
        return None
    action = (item.text("TFERACTION") or "").strip().upper()
    if action not in ("IN", "OUT"):
        action = "IN" if units > 0 else "OUT"
    return [
        InvestmentActivity(
            type="transfer_in" if action == "IN" else "transfer_out",
            trade_date=trade_date,
            security=security,
            quantity=abs(units),
            unit_price=_quantity(item.text("UNITPRICE")),
            amount=Decimal(0),
            fitid=fitid,
            memo=memo,
        )
    ]


def _split(item: OfxNode, catalogue: _Catalogue) -> list[InvestmentActivity] | None:
    trade_date, _, fitid, memo = _invtran(item)
    old = _quantity(item.text("OLDUNITS"))
    new = _quantity(item.text("NEWUNITS"))
    security = catalogue.ref(item.child("SECID"), None)
    if trade_date is None or old is None or new is None or security is None:
        return None
    if old <= 0 or new <= 0 or old == new:
        return None
    return [
        InvestmentActivity(
            type="split",
            trade_date=trade_date,
            security=security,
            split_ratio=(new, old),
            amount=Decimal(0),
            fitid=fitid,
            memo=memo,
        )
    ]


def _bank(item: OfxNode) -> list[InvestmentActivity] | None:
    stmttrn = item.child("STMTTRN")
    if stmttrn is None:
        return None
    posted = _date(stmttrn.text("DTPOSTED"))
    amount = parse_amount(stmttrn.text("TRNAMT"))
    if posted is None or amount is None or amount == 0:
        return None
    memo = clean_text(stmttrn.text("NAME")) or clean_text(stmttrn.text("MEMO"))
    return [
        InvestmentActivity(
            type="deposit" if amount > 0 else "withdrawal",
            trade_date=posted,
            amount=amount,
            fitid=clean_text(stmttrn.text("FITID"), limit=255),
            memo=memo,
        )
    ]


def _activity_of(item: OfxNode, catalogue: _Catalogue) -> list[InvestmentActivity] | None:
    readers: dict[str, Callable[[], list[InvestmentActivity] | None]] = {
        "INCOME": lambda: _income(item, catalogue),
        "REINVEST": lambda: _reinvest(item, catalogue),
        "INVEXPENSE": lambda: _expense(item, catalogue),
        "TRANSFER": lambda: _transfer(item, catalogue),
        "SPLIT": lambda: _split(item, catalogue),
        "INVBANKTRAN": lambda: _bank(item),
    }
    if item.name in _BUY:
        return _trade(item, "INVBUY", "buy", catalogue)
    if item.name in _SELL:
        return _trade(item, "INVSELL", "sell", catalogue)
    reader = readers.get(item.name)
    return reader() if reader is not None else None


def _statement(
    node: OfxNode,
    kind: str,
    catalogue: _Catalogue,
    options: ExtractOptions,
    budget: list[int],
    warnings: set[str],
    skipped: list[int],
    institution: str | None,
) -> InvestmentStatement:
    account_node = node.child("INVACCTFROM")
    number = account_node.text("ACCTID") if account_node is not None else None
    broker = account_node.text("BROKERID") if account_node is not None else None
    tranlist = node.child("INVTRANLIST")
    period_start = _date(tranlist.text("DTSTART")) if tranlist is not None else None
    period_end = _date(tranlist.text("DTEND")) if tranlist is not None else None
    poslist = node.child("INVPOSLIST")
    as_of = (
        (_date(poslist.text("DTASOF")) if poslist is not None else None)
        or _date(node.text("DTASOF"))
        or period_end
    )
    activity: list[InvestmentActivity] = []
    truncated = False
    unknown = 0
    unread = 0
    if tranlist is not None:
        for item in tranlist.children:
            if item.name in ("DTSTART", "DTEND"):
                continue
            if item.name not in _KNOWN:
                unknown += 1
                # An aggregate is a record (it carries an ``INVTRAN`` at
                # least) and was not imported; a lone leaf is not one.
                if item.children:
                    unread += 1
                continue
            if budget[0] <= 0:
                truncated = True
                break
            try:
                rows = _activity_of(item, catalogue)
            except ValidationError:
                rows = None
            if rows is None:
                unread += 1
                continue
            activity.extend(rows)
            budget[0] -= 1
    if unknown:
        warnings.add("investment_activity_kind_unsupported")
    # Activity beside the list (a stray ``</INVTRANLIST>`` closed it early) is not read.
    unread += sum(1 for item in node.children if item.name in _KNOWN)
    skipped.append(unread)
    if truncated:
        warnings.add(f"transactions_truncated:{options.max_transactions}")
    cash: Balance | None = None
    invbal = node.child("INVBAL")
    if invbal is not None:
        available = parse_amount(invbal.text("AVAILCASH"))
        if available is not None:
            cash = Balance(amount=available, as_of=as_of)
    currency = _currency(node.text("CURDEF"))
    plan = any(node.child(name) is not None for name in _PLAN_401K)
    return InvestmentStatement(
        source_format="qfx" if kind == "qfx" else "ofx",
        account=StatementAccount(
            account_type="retirement" if plan else "brokerage",
            subtype="401k" if plan else None,
            last4=last4(number),
            currency=currency,
            institution=institution or clean_text(broker, limit=200),
        ),
        period_start=period_start,
        period_end=period_end,
        as_of=as_of,
        positions=_positions(node, catalogue, as_of, warnings),
        activity=activity,
        cash_closing=cash,
        truncated=truncated,
    )


def investment_statements(
    root: OfxNode,
    kind: str,
    options: ExtractOptions,
    budget: list[int],
    warnings: set[str],
    skipped: list[int],
    institution: str | None,
) -> list[InvestmentStatement]:
    """Every ``INVSTMTRS`` of the file (module doc); ``budget`` caps activity rows.

    ``skipped`` receives, per statement, how many activity records were not
    read (module doc).
    """
    nodes = list(root.iter("INVSTMTRS"))
    if not nodes:
        return []
    catalogue = _Catalogue(root)
    return [
        _statement(node, kind, catalogue, options, budget, warnings, skipped, institution)
        for node in nodes[:MAX_STATEMENTS]
    ]
