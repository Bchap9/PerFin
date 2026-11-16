# SPDX-License-Identifier: AGPL-3.0-or-later
"""The merchant dictionary that ships with PerFin.

Static data inside the package (``data/``), read on this host: looking a
merchant up makes no request to anything. :func:`lookup` finds the merchant a
descriptor's core names, :func:`keyword` a word in it that says what was
bought.

**The files** (``data/README.md`` is the reference for whoever edits them):

* ``meta.yaml``: ``version`` (a household evaluated with an older one is
  refreshed) and ``digest`` (what :func:`digest` returns for that version;
  a unit test holds the two together).
* ``merchants/<sector>.yaml``: ``merchants:``, a list of entries with
  ``key``, ``name``, ``category``, ``match`` and optionally ``series``,
  ``cadences``, ``mixed`` and ``inflow`` (:class:`Merchant`).
* ``keywords.yaml``: ``keywords:``, a list of entries with ``word``,
  ``category`` and optionally ``strong`` and ``via``.

**Matching** is by whole tokens and by nothing else: no regular expression is
built from an entry or from a core. A ``match`` is one or more tokens in
merchant-key form; it is looked for at the start of the core, or anywhere in
it when it ends in the marker `` ~``, or it must be the whole core when it
ends in the marker `` =`` (a name other businesses carry too: a word, a
surname, a place). The match with the most tokens wins, so a sub-brand beats
its brand; when the longest matches belong to different merchants the core
names nobody.

**A payment to the merchant's card** is not a purchase. When the rest of the
core speaks of a credit card or of card services, or says "payment" after a
merchant that has no ``series`` (a shop is paid at the till; a bill is
paid), the core names nobody: the row is left to the kind clean-up gives it
and to the person.

Everything is read once per process, with ``yaml.safe_load``, and checked on
the way in: a malformed file raises :class:`DictionaryError` at the first
call, naming the file and the entry.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from types import MappingProxyType
from typing import Final, Literal, cast

import yaml

from app.domain.transactions.categories import default_category_keys

DATA_PACKAGE: Final = "app.domain.categorisation"
DATA_DIR: Final = "data"
META_FILE: Final = "meta.yaml"
KEYWORDS_FILE: Final = "keywords.yaml"
MERCHANTS_DIR: Final = "merchants"

#: Merchant-key form: lower-case ``a-z0-9`` tokens joined by single spaces.
#: A constant used to check entries; it is never built from one.
KEY_FORM: Final = re.compile(r"[a-z0-9]+(?: [a-z0-9]+)*")
KEY_MAX: Final = 64
NAME_MAX: Final = 80
#: A ``match`` ending in this is looked for anywhere in the core.
ANYWHERE_MARK: Final = " ~"
#: A ``match`` ending in this must be the whole core.
ALONE_MARK: Final = " ="
#: Where a match is read: at the start of the core, anywhere in it, or as all of it.
Where = Literal["start", "anywhere", "alone"]
_MARKS: Final[Mapping[str, Where]] = MappingProxyType(
    {ANYWHERE_MARK: "anywhere", ALONE_MARK: "alone"}
)
_MARK_OF: Final[Mapping[str, str]] = MappingProxyType(
    {"start": "", "anywhere": ANYWHERE_MARK, "alone": ALONE_MARK}
)
#: Words that say an account was paid, not that something was bought.
PAYMENT_WORDS: Final = frozenset(
    {
        "payment",
        "payments",
        "pmt",
        "pymt",
        "pymnt",
        "paymt",
        "epay",
        "epayment",
        "autopay",
        "autopmt",
        "autopymt",
    }
)
_CARD_WORDS: Final = frozenset({"card", "crd"})
_SERVICE_WORDS: Final = frozenset({"service", "services", "srvc", "svc", "svcs"})
#: Tokens in a keyword's ``word``, at most.
WORD_TOKENS_MAX: Final = 2
#: The cadences an entry may name: how often its series usually bills.
CADENCES: Final = (
    "weekly",
    "biweekly",
    "monthly",
    "two_monthly",
    "quarterly",
    "half_yearly",
    "annual",
)

How = Literal["exact", "prefix", "anywhere"]
_RANK: Final[Mapping[str, int]] = MappingProxyType({"exact": 0, "prefix": 1, "anywhere": 2})
_MERCHANT_FIELDS: Final = frozenset(
    {"key", "name", "category", "match", "series", "cadences", "mixed", "inflow"}
)
_KEYWORD_FIELDS: Final = frozenset({"word", "category", "strong", "via"})
_DIGEST_FORM: Final = re.compile(r"[0-9a-f]{64}")


class DictionaryError(ValueError):
    """A dictionary file that cannot be used; the message names the file and the entry."""


@dataclass(frozen=True)
class Merchant:
    """One entry: who it is and the default category its charges get."""

    key: str
    name: str
    #: A default category's ``system_key``.
    category: str
    #: Its charges are usually a recurring series of this kind.
    series: Literal["subscription", "bill"] | None = None
    cadences: tuple[str, ...] = ()
    #: A store that sells everything: its category is only ever a suggestion.
    mixed: bool = False
    #: Pays money in (a payroll processor).
    inflow: bool = False


@dataclass(frozen=True)
class Hit:
    """A merchant found in a core, and how: the whole core, its start, or inside it."""

    merchant: Merchant
    how: How


@dataclass(frozen=True)
class KeywordHit:
    """A word that says what was bought; ``strong`` ones are sure enough to state."""

    category: str
    word: str
    strong: bool


@dataclass(frozen=True)
class _Match:
    tokens: tuple[str, ...]
    merchant: Merchant
    #: Counts only when it is the whole core.
    alone: bool = False


@dataclass(frozen=True)
class _Word:
    #: Position in the file: the first one found wins.
    order: int
    #: Empty for a keyword that covers everything behind its processor.
    tokens: tuple[str, ...]
    hit: KeywordHit


def _at(tokens: tuple[str, ...], start: int, wanted: tuple[str, ...]) -> bool:
    return tokens[start : start + len(wanted)] == wanted


def _pays_an_account(rest: tuple[str, ...], merchant: Merchant) -> bool:
    """Whether ``rest`` (the core without the merchant's name) says its card was paid.

    "Credit card", "credit services" and "card services" or "card payment"
    say so of anyone. A bare payment word says so of a merchant without a
    ``series`` only: a utility's "payment" is the bill itself.
    """
    for word, following in itertools.pairwise(rest):
        if word == "credit" and (following in _CARD_WORDS or following in _SERVICE_WORDS):
            return True
        if word in _CARD_WORDS and (following in _SERVICE_WORDS or following in PAYMENT_WORDS):
            return True
    return merchant.series is None and not PAYMENT_WORDS.isdisjoint(rest)


@dataclass(frozen=True)
class Table:
    """The dictionary, indexed: built by :func:`build`, served by the module's functions."""

    version: int
    merchants: Mapping[str, Merchant]
    #: SHA-256 of the canonical JSON of every entry (:func:`digest`).
    digest: str
    #: Matches by their first token: those read at the start of a core, ...
    _prefix: Mapping[str, tuple[_Match, ...]] = field(repr=False)
    #: ... and those read anywhere in it.
    _anywhere: Mapping[str, tuple[_Match, ...]] = field(repr=False)
    #: Keywords by their first token, and those tied to a processor by its name.
    _words: Mapping[str, tuple[_Word, ...]] = field(repr=False)
    _via: Mapping[str, tuple[_Word, ...]] = field(repr=False)

    def lookup(self, core: str) -> Hit | None:
        """The merchant ``core`` names, or ``None`` (module doc)."""
        tokens = tuple(core.split())
        longest = 0
        #: By merchant key: the hit and where its match starts.
        found: dict[str, tuple[Hit, int]] = {}
        for start, token in enumerate(tokens):
            candidates = self._anywhere.get(token, ())
            if start == 0:
                candidates = (*self._prefix.get(token, ()), *candidates)
            for match in candidates:
                size = len(match.tokens)
                if size < longest or not _at(tokens, start, match.tokens):
                    continue
                if match.alone and size != len(tokens):
                    # Something else is named with it: not this merchant's claim.
                    continue
                if size > longest:
                    longest = size
                    found = {}
                how: How = (
                    "exact" if size == len(tokens) else "prefix" if start == 0 else "anywhere"
                )
                known = found.get(match.merchant.key)
                if known is None or _RANK[how] < _RANK[known[0].how]:
                    found[match.merchant.key] = (Hit(merchant=match.merchant, how=how), start)
        if len(found) != 1:
            # Nobody, or two merchants with equally long claims.
            return None
        hit, start = next(iter(found.values()))
        if _pays_an_account((*tokens[:start], *tokens[start + longest :]), hit.merchant):
            return None
        return hit

    def keyword(self, core: str, via: str | None) -> KeywordHit | None:
        """A telling word in ``core`` (or behind the processor ``via``), or ``None``.

        The keywords tied to ``via`` are read first, then the others; in each
        group the first in file order that is in the core wins.
        """
        tokens = tuple(core.split())
        if via is not None:
            for word in self._via.get(via, ()):
                if not word.tokens or any(
                    _at(tokens, start, word.tokens) for start in range(len(tokens))
                ):
                    return word.hit
        first: _Word | None = None
        for start, token in enumerate(tokens):
            for word in self._words.get(token, ()):
                if (first is None or word.order < first.order) and _at(tokens, start, word.tokens):
                    first = word
        return first.hit if first is not None else None


# --- reading and checking ------------------------------------------------------


def parse(name: str, text: str) -> object:
    """``text`` as YAML, read with ``safe_load``: plain data, never an object."""
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as error:
        problem = " ".join(str(error).split())
        raise DictionaryError(f"{name}: not YAML this dictionary can read ({problem})") from error


def _is_key(value: object) -> bool:
    return isinstance(value, str) and len(value) <= KEY_MAX and bool(KEY_FORM.fullmatch(value))


def _entries(file: str, document: object, top: str) -> list[object]:
    """The list under ``top``, which is all a file may hold."""
    if not isinstance(document, dict) or set(document) != {top}:
        raise DictionaryError(f"{file}: the file must hold `{top}:` and nothing else")
    entries = document[top]
    if not isinstance(entries, list):
        raise DictionaryError(f"{file}: `{top}` must be a list")
    return entries


class _Entry:
    """One entry being checked: every complaint names the file and the entry."""

    def __init__(self, file: str, number: int, raw: object, allowed: frozenset[str]) -> None:
        self.where = f"{file}: entry {number}"
        if not isinstance(raw, dict) or not all(isinstance(name, str) for name in raw):
            raise self.error("must be a mapping of fields")
        self.raw: dict[str, object] = raw
        label = raw.get("key", raw.get("word"))
        if isinstance(label, str | int | float):
            self.where = f"{file}: entry {number} ({str(label)[:KEY_MAX]!r})"
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise self.error(f"unknown field `{unknown[0]}`")

    def error(self, problem: str) -> DictionaryError:
        return DictionaryError(f"{self.where}: {problem}")

    def key(self, name: str) -> str:
        value = self.raw.get(name)
        if not _is_key(value):
            raise self.error(
                f"`{name}` must be lower-case a-z0-9 tokens joined by single spaces, "
                f"at most {KEY_MAX} characters"
            )
        return cast(str, value)

    def category(self, known: frozenset[str]) -> str:
        value = self.raw.get("category")
        if not isinstance(value, str) or value not in known:
            raise self.error(f"`category` {value!r} is not a default category key")
        return value

    def flag(self, name: str) -> bool:
        value = self.raw.get(name, False)
        if not isinstance(value, bool):
            raise self.error(f"`{name}` must be true or false")
        return value


def _merchant(
    entry: _Entry, categories: frozenset[str]
) -> tuple[Merchant, list[tuple[str, Where]]]:
    """The merchant an entry describes and its matches as ``(tokens, where)``."""
    raw = entry.raw
    key = entry.key("key")
    name = raw.get("name")
    if (
        not isinstance(name, str)
        or not name
        or name != name.strip()
        or len(name) > NAME_MAX
        or not name.isprintable()
    ):
        raise entry.error(f"`name` must be printable text of 1 to {NAME_MAX} characters")
    category = entry.category(categories)

    written = raw.get("match")
    if not isinstance(written, list) or not written:
        raise entry.error("`match` must be a list of at least one string")
    matches: list[tuple[str, Where]] = []
    for item in written:
        plain: object = item
        where: Where = "start"
        if isinstance(item, str):
            for mark, marked in _MARKS.items():
                if item.endswith(mark):
                    plain, where = item.removesuffix(mark), marked
                    break
        if not _is_key(plain):
            raise entry.error(
                f"`match` {item!r} must be lower-case a-z0-9 tokens joined by single "
                f"spaces, with `{ANYWHERE_MARK.strip()}` after a space at the end to match "
                f"anywhere or `{ALONE_MARK.strip()}` to match the whole core only"
            )
        matches.append((cast(str, plain), where))
    if len({plain for plain, _ in matches}) != len(matches):
        raise entry.error("`match` names the same tokens twice")

    series = raw.get("series")
    if series not in (None, "subscription", "bill"):
        raise entry.error("`series` must be subscription or bill")
    cadences = raw.get("cadences", [])
    if (
        not isinstance(cadences, list)
        or not all(isinstance(item, str) and item in CADENCES for item in cadences)
        or len(set(cadences)) != len(cadences)
    ):
        raise entry.error(f"`cadences` must be a list of distinct names from {', '.join(CADENCES)}")
    if cadences and series is None:
        raise entry.error("`cadences` needs a `series`")

    merchant = Merchant(
        key=key,
        name=name,
        category=category,
        series=series,
        cadences=tuple(cadences),
        mixed=entry.flag("mixed"),
        inflow=entry.flag("inflow"),
    )
    return merchant, matches


def _keyword(entry: _Entry, order: int, categories: frozenset[str]) -> tuple[_Word, str | None]:
    """The keyword an entry describes and the processor it is tied to, if any."""
    raw = entry.raw
    via: str | None = None
    if "via" in raw:
        via = entry.key("via")
        if " " in via:
            raise entry.error("`via` must be one token, the name clean-up gives a processor")
    tokens: tuple[str, ...] = ()
    if "word" in raw or via is None:
        tokens = tuple(entry.key("word").split())
        if len(tokens) > WORD_TOKENS_MAX:
            raise entry.error(f"`word` must be at most {WORD_TOKENS_MAX} tokens")
    category = entry.category(categories)
    # A keyword with no word covers everything behind its processor, and is told by its name.
    word = " ".join(tokens) if tokens else cast(str, via)
    hit = KeywordHit(category=category, word=word, strong=entry.flag("strong"))
    return _Word(order=order, tokens=tokens, hit=hit), via


def _digest(
    merchants: Mapping[str, Merchant],
    matches: Mapping[str, list[tuple[str, Where]]],
    keywords: list[tuple[_Word, str | None]],
) -> str:
    """SHA-256 of the entries as canonical JSON.

    Merchants are sorted by key and their matches by text, so moving an entry
    between files or lines changes nothing; keywords stay in file order,
    because their order decides which one answers.
    """
    content = {
        "merchants": [
            {
                "key": merchant.key,
                "name": merchant.name,
                "category": merchant.category,
                "match": sorted(plain + _MARK_OF[where] for plain, where in matches[merchant.key]),
                "series": merchant.series,
                "cadences": list(merchant.cadences),
                "mixed": merchant.mixed,
                "inflow": merchant.inflow,
            }
            for _, merchant in sorted(merchants.items())
        ],
        "keywords": [
            {
                "word": " ".join(word.tokens) or None,
                "category": word.hit.category,
                "strong": word.hit.strong,
                "via": via,
            }
            for word, via in keywords
        ],
    }
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _by_first(items: list[tuple[str, _Match]]) -> Mapping[str, tuple[_Match, ...]]:
    grouped: dict[str, list[_Match]] = {}
    for first, match in items:
        grouped.setdefault(first, []).append(match)
    return MappingProxyType({first: tuple(found) for first, found in grouped.items()})


def build(
    merchant_files: Mapping[str, object],
    keywords: object,
    *,
    version: int,
    keywords_file: str = KEYWORDS_FILE,
) -> Table:
    """Check parsed files and index them.

    ``merchant_files`` maps a file's name to its parsed content, ``keywords``
    is the parsed keyword file. Files are read in the order of their names.
    Raises :class:`DictionaryError` for anything the format does not allow,
    for a key used twice, for tokens that match two merchants, and for a
    merchant matched by a keyword.
    """
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise DictionaryError(f"{META_FILE}: `version` must be a whole number, 1 or more")
    categories = frozenset(default_category_keys())

    merchants: dict[str, Merchant] = {}
    matches: dict[str, list[tuple[str, Where]]] = {}
    seen_in: dict[str, str] = {}
    owner: dict[str, str] = {}
    prefix: list[tuple[str, _Match]] = []
    anywhere: list[tuple[str, _Match]] = []
    for file in sorted(merchant_files):
        listed = _entries(file, merchant_files[file], "merchants")
        if not listed:
            raise DictionaryError(f"{file}: no merchants")
        for number, raw in enumerate(listed, start=1):
            entry = _Entry(file, number, raw, _MERCHANT_FIELDS)
            merchant, wanted = _merchant(entry, categories)
            if merchant.key in merchants:
                raise entry.error(f"the key is already used in {seen_in[merchant.key]}")
            for plain, _ in wanted:
                if plain in owner:
                    raise entry.error(
                        f"`match` {plain!r} already belongs to {owner[plain]!r} "
                        f"in {seen_in[owner[plain]]}"
                    )
                owner[plain] = merchant.key
            merchants[merchant.key] = merchant
            matches[merchant.key] = wanted
            seen_in[merchant.key] = entry.where
            for plain, where in wanted:
                tokens = tuple(plain.split())
                (anywhere if where == "anywhere" else prefix).append(
                    (tokens[0], _Match(tokens=tokens, merchant=merchant, alone=where == "alone"))
                )

    words: dict[str, list[_Word]] = {}
    behind: dict[str, list[_Word]] = {}
    read: list[tuple[_Word, str | None]] = []
    for number, raw in enumerate(_entries(keywords_file, keywords, "keywords"), start=1):
        entry = _Entry(keywords_file, number, raw, _KEYWORD_FIELDS)
        word, via = _keyword(entry, number, categories)
        text = " ".join(word.tokens)
        if text in owner:
            raise entry.error(
                f"`word` {text!r} is a keyword and a `match` of {owner[text]!r} "
                f"in {seen_in[owner[text]]}"
            )
        read.append((word, via))
        if via is not None:
            behind.setdefault(via, []).append(word)
        else:
            words.setdefault(word.tokens[0], []).append(word)

    return Table(
        version=version,
        merchants=MappingProxyType(merchants),
        digest=_digest(merchants, matches, read),
        _prefix=_by_first(prefix),
        _anywhere=_by_first(anywhere),
        _words=MappingProxyType({first: tuple(found) for first, found in words.items()}),
        _via=MappingProxyType({via: tuple(found) for via, found in behind.items()}),
    )


@cache
def _meta() -> tuple[int, str]:
    """``version`` and ``digest`` as ``meta.yaml`` states them."""
    data = resources.files(DATA_PACKAGE).joinpath(DATA_DIR)
    meta = parse(META_FILE, data.joinpath(META_FILE).read_text(encoding="utf-8"))
    if not isinstance(meta, dict) or set(meta) != {"version", "digest"}:
        raise DictionaryError(f"{META_FILE}: the file must hold `version` and `digest`")
    version, stated = meta["version"], meta["digest"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise DictionaryError(f"{META_FILE}: `version` must be a whole number, 1 or more")
    if not isinstance(stated, str) or not _DIGEST_FORM.fullmatch(stated):
        raise DictionaryError(f"{META_FILE}: `digest` must be 64 lower-case hex characters")
    return version, stated


@cache
def table() -> Table:
    """The shipped dictionary, read and checked once per process."""
    data = resources.files(DATA_PACKAGE).joinpath(DATA_DIR)
    files = sorted(
        (
            item
            for item in data.joinpath(MERCHANTS_DIR).iterdir()
            if item.is_file() and item.name.endswith(".yaml")
        ),
        key=lambda item: item.name,
    )
    if not files:
        raise DictionaryError(f"{MERCHANTS_DIR}: no merchant file ships with this build")
    merchant_files = {
        f"{MERCHANTS_DIR}/{item.name}": parse(
            f"{MERCHANTS_DIR}/{item.name}", item.read_text(encoding="utf-8")
        )
        for item in files
    }
    keywords = parse(KEYWORDS_FILE, data.joinpath(KEYWORDS_FILE).read_text(encoding="utf-8"))
    return build(merchant_files, keywords, version=_meta()[0])


# --- what the other modules call -------------------------------------------------


def version() -> int:
    """The dictionary's version; a household evaluated with an older one is refreshed."""
    return table().version


def merchants() -> Mapping[str, Merchant]:
    """Every merchant by key."""
    return table().merchants


def lookup(core: str) -> Hit | None:
    """The merchant ``core`` names, or ``None``."""
    return table().lookup(core)


def keyword(core: str, via: str | None) -> KeywordHit | None:
    """A telling word in ``core`` (or behind the processor ``via``), or ``None``."""
    return table().keyword(core, via)


def name_for(key: str) -> str | None:
    """The display name of the merchant with this key, or ``None`` when it has no entry."""
    merchant = table().merchants.get(key)
    return merchant.name if merchant is not None else None


def digest() -> str:
    """SHA-256 of the canonical JSON of all entries; ``meta.yaml`` pins it to the version."""
    return table().digest
