# SPDX-License-Identifier: AGPL-3.0-or-later
"""The bundled merchant dictionary: its gate, its loader and its matcher.

The first tests read the data files as they ship and are the review gate of
the design note (section 2): the schema, the token form, unique keys, a match
owned by one merchant, every category a default key, and a content digest
pinned to the version. The others pin how a core is matched, mostly with
small tables built here so that they do not depend on what the files hold.

Merchant names in the shipped files are real brand names, which is intended;
every name in a table built here is invented.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from importlib import resources
from typing import Any

import pytest
import yaml

from app.domain.categorisation import cleanup, dictionary
from app.domain.categorisation.dictionary import (
    DictionaryError,
    Hit,
    KeywordHit,
    Merchant,
    Table,
)
from app.domain.transactions.categories import default_category_keys

KEY_FORM = re.compile(r"[a-z0-9]+( [a-z0-9]+)*")
KEY_MAX = 64
MERCHANT_FIELDS = {"key", "name", "category", "match", "series", "cadences", "mixed", "inflow"}
KEYWORD_FIELDS = {"word", "category", "strong", "via"}
CADENCES = {
    "weekly",
    "biweekly",
    "monthly",
    "two_monthly",
    "quarterly",
    "half_yearly",
    "annual",
}

#: What clean-up strips as a processor or marketplace prefix (the plan's Task
#: 5 table), folded to tokens: a merchant may not be matched by these letters.
PROCESSOR_LETTERS = frozenset(
    {
        "sq",
        "sq sq",
        "gosq com",
        "tst",
        "paypal",
        "pp",
        "dd",
        "doordash",
        "ic",
        "gh",
        "grubhub",
        "paddle net",
        "fs",
        "dri",
        "bt",
        "wpy",
        "in",
        "eb",
        "act",
        "abc",
        "aci",
        "sp",
    }
)
#: The ``via`` values clean-up gives those prefixes.
PROCESSOR_NAMES = frozenset(
    {
        "square",
        "toast",
        "paypal",
        "doordash",
        "instacart",
        "grubhub",
        "paddle",
        "fastspring",
        "digitalriver",
        "braintree",
        "wepay",
        "intuit",
        "eventbrite",
        "active",
        "abc",
        "aci",
        "shopify",
    }
)


def _fold(text: str) -> str:
    return " ".join("".join(ch if ch.isalnum() else " " for ch in text.lower()).split())


def _processors() -> tuple[frozenset[str], frozenset[str]]:
    """The written lists, and clean-up's own table once it has one."""
    table: tuple[tuple[str, str], ...] = getattr(cleanup, "PROCESSORS", ())
    letters = PROCESSOR_LETTERS | {_fold(prefix) for prefix, _ in table}
    names = PROCESSOR_NAMES | {via for _, via in table}
    return frozenset(letters), frozenset(names)


def _data() -> Any:
    return resources.files("app.domain.categorisation").joinpath("data")


def _shipped_merchants() -> list[tuple[str, dict[str, Any]]]:
    """Every merchant entry as written, with the file it is in."""
    found: list[tuple[str, dict[str, Any]]] = []
    files = sorted(
        (item for item in _data().joinpath("merchants").iterdir() if item.name.endswith(".yaml")),
        key=lambda item: item.name,
    )
    assert files, "no merchant file ships"
    for item in files:
        document = yaml.safe_load(item.read_text(encoding="utf-8"))
        assert set(document) == {"merchants"}, item.name
        assert isinstance(document["merchants"], list), item.name
        assert document["merchants"], item.name
        found.extend((item.name, entry) for entry in document["merchants"])
    return found


def _shipped_keywords() -> list[dict[str, Any]]:
    document = yaml.safe_load(_data().joinpath("keywords.yaml").read_text(encoding="utf-8"))
    assert set(document) == {"keywords"}
    assert isinstance(document["keywords"], list)
    return list(document["keywords"])


def _is_key(value: object) -> bool:
    return isinstance(value, str) and len(value) <= KEY_MAX and bool(KEY_FORM.fullmatch(value))


def _plain(match: str) -> str:
    return match.removesuffix(" ~")


# --- the gate ----------------------------------------------------------------


def test_every_entry_is_well_formed() -> None:
    categories = set(default_category_keys())
    letters, names = _processors()
    keywords = _shipped_keywords()
    words = {entry["word"] for entry in keywords if "word" in entry}

    owners: dict[str, str] = {}
    keys: set[str] = set()
    for file, entry in _shipped_merchants():
        where = f"{file}: {entry.get('key')!r}"
        assert isinstance(entry, dict), where
        assert set(entry) <= MERCHANT_FIELDS, where
        assert {"key", "name", "category", "match"} <= set(entry), where
        assert _is_key(entry["key"]), where
        assert entry["key"] not in keys, f"{where}: the key is used twice"
        keys.add(entry["key"])
        assert isinstance(entry["name"], str), where
        assert entry["name"] == entry["name"].strip() != "", where
        assert entry["name"].isprintable(), where
        assert entry["category"] in categories, f"{where}: {entry['category']!r} is no default key"
        assert isinstance(entry["match"], list), where
        assert entry["match"], where
        for match in entry["match"]:
            assert isinstance(match, str), where
            plain = _plain(match)
            assert _is_key(plain), f"{where}: match {match!r}"
            assert plain not in owners, f"{where}: {plain!r} also matches {owners.get(plain)!r}"
            owners[plain] = entry["key"]
            assert plain not in words, f"{where}: {plain!r} is a keyword"
            assert plain not in letters, f"{where}: {plain!r} is a processor's letters"
        assert entry.get("series") in (None, "subscription", "bill"), where
        cadences = entry.get("cadences", [])
        assert isinstance(cadences, list), where
        assert set(cadences) <= CADENCES, f"{where}: {cadences!r}"
        assert len(set(cadences)) == len(cadences), where
        assert not cadences or "series" in entry, f"{where}: cadences without a series"
        for flag in ("mixed", "inflow"):
            assert entry.get(flag, True) is True, f"{where}: {flag} is written only as true"

    for entry in keywords:
        where = f"keywords.yaml: {entry!r}"
        assert isinstance(entry, dict), where
        assert set(entry) <= KEYWORD_FIELDS, where
        assert entry["category"] in categories, where
        assert entry.get("strong", True) is True, where
        if "via" in entry:
            assert entry["via"] in names, f"{where}: no such processor"
        else:
            assert "word" in entry, where
        if "word" in entry:
            assert _is_key(entry["word"]), where
            assert len(entry["word"].split()) <= 2, where

    # What the loader serves is what the files say.
    served = dictionary.merchants()
    assert set(served) == keys
    assert all(key == merchant.key for key, merchant in served.items())
    assert len(served) >= 40
    assert dictionary.version() >= 1


def test_the_digest_is_pinned_to_the_version() -> None:
    meta = yaml.safe_load(_data().joinpath("meta.yaml").read_text(encoding="utf-8"))
    assert set(meta) == {"version", "digest"}
    assert meta["version"] == dictionary.version()
    assert re.fullmatch(r"[0-9a-f]{64}", dictionary.digest())
    assert meta["digest"] == dictionary.digest(), (
        "the dictionary's entries changed: bump `version` and update `digest` in "
        f"data/meta.yaml (the digest is now {dictionary.digest()})"
    )


def test_the_digest_follows_the_entries_and_not_the_file_they_are_in() -> None:
    one = _table({"a.yaml": [STREAMBOX, EVERYMART]})
    moved = _table({"a.yaml": [EVERYMART], "b.yaml": [STREAMBOX]})
    renamed = _table({"a.yaml": [{**STREAMBOX, "name": "Stream Box"}, EVERYMART]})
    rematched = _table({"a.yaml": [{**STREAMBOX, "match": ["streambox ~"]}, EVERYMART]})
    assert one.digest == moved.digest
    assert len({one.digest, renamed.digest, rematched.digest}) == 3
    # Keywords are read in order, so their order is content.
    words = [
        {"word": "pizza", "category": "food.restaurants"},
        {"word": "taco", "category": "food"},
    ]
    assert (
        _table({"a.yaml": [STREAMBOX]}, words).digest
        != _table({"a.yaml": [STREAMBOX]}, words[::-1]).digest
    )


# --- the matcher, on the shipped files ---------------------------------------


def test_lookup_prefix_and_exact() -> None:
    netflix = dictionary.merchants()["netflix"]
    assert netflix.name == "Netflix"
    assert netflix.category == "leisure.subscriptions"
    assert netflix.series == "subscription"
    assert "monthly" in netflix.cadences
    assert dictionary.lookup("netflix") == Hit(merchant=netflix, how="exact")
    assert dictionary.lookup("netflix com") == Hit(merchant=netflix, how="prefix")
    assert dictionary.name_for("netflix") == "Netflix"
    assert dictionary.name_for("lantern books") is None
    assert dictionary.lookup("lantern books") is None
    assert dictionary.lookup("") is None


def test_whole_tokens_only() -> None:
    assert dictionary.lookup("netflixx") is None
    assert dictionary.lookup("netfli") is None
    assert dictionary.lookup("mynetflix") is None
    # A two-token match is not found in one squashed token, nor across a gap.
    table = _table({"a.yaml": [LANTERN]})
    assert table.lookup("lantern books") is not None
    assert table.lookup("lanternbooks") is None
    assert table.lookup("lantern used books") is None
    assert table.lookup("lantern") is None


# --- what the shipped files hold: everyday merchants ---------------------------

#: Merchants each sector file must hold, at least.
SECTOR_FLOORS = {
    "groceries.yaml": 60,
    "restaurants.yaml": 120,
    "fuel_transport.yaml": 50,
    "shopping.yaml": 120,
    "health.yaml": 40,
    "travel.yaml": 50,
    "home.yaml": 40,
}

#: A store that sells everything, by a core a statement gives it and its key.
MIXED_STORES = (
    ("amzn mktp us", "amazon"),
    ("wal mart", "walmart"),
    ("wm supercenter", "walmart"),
    ("target", "target"),
    ("costco whse", "costco"),
    ("samsclub", "sams club"),
    ("bjs wholesale", "bjs wholesale club"),
    ("meijer", "meijer"),
    ("fred meyer", "fred meyer"),
    ("dollar general", "dollar general"),
    ("dollar tree", "dollar tree"),
    ("family dollar", "family dollar"),
    ("ebay o 12 34567", "ebay"),
    ("temu", "temu"),
)

#: A brand's core and key, then its sub-brand's core, key and category.
SUB_BRANDS = (
    ("costco whse", "costco", "costco gas", "costco gas", "transport.fuel"),
    ("costco whse", "costco", "costco annual renewal", "costco membership", "leisure.memberships"),
    ("sam club", "sams club", "sam club fuel", "sams club fuel", "transport.fuel"),
    ("bjs wholesale", "bjs wholesale club", "bjs fuel", "bjs fuel", "transport.fuel"),
    ("kroger", "kroger", "kroger fuel", "kroger fuel", "transport.fuel"),
    ("kroger", "kroger", "kroger pharmacy", "kroger pharmacy", "health.pharmacy"),
    ("safeway", "safeway", "safeway fuel", "safeway fuel", "transport.fuel"),
    ("uber trip", "uber", "uber eats", "uber eats", "food.delivery"),
    ("amazon", "amazon", "amazon prime", "amazon prime", "leisure.memberships"),
    ("amzn mktp us", "amazon", "amazon fresh", "amazon fresh", "food.groceries"),
)

#: Paying a person, paying a card, or a bank: never a merchant.
NOT_MERCHANTS = (
    "venmo",
    "venmo payment",
    "zelle",
    "zelle payment",
    "cash app",
    "paypal",
    "apple cash",
    "chase",
    "chase credit crd",
    "chase card",
    "capital one",
    "capital one mobile pmt",
    "amex epayment",
    "american express",
    "discover",
    "discover e payment",
    "citi",
    "citi card online",
    "barclaycard us",
    "synchrony bank",
    "bank of america",
    "wells fargo",
    "us bank",
    "pnc bank",
    "td bank",
    "navy federal",
    "ally bank",
    "sofi",
    "chime",
)


def test_size_after_everyday_merchants() -> None:
    assert len(dictionary.merchants()) >= 600


def test_sector_floors() -> None:
    held: dict[str, int] = {}
    for file, _ in _shipped_merchants():
        held[file] = held.get(file, 0) + 1
    short = {
        file: (held.get(file, 0), floor)
        for file, floor in SECTOR_FLOORS.items()
        if held.get(file, 0) < floor
    }
    assert not short, f"sector files below their floor (held, floor): {short}"
    # The two smaller sectors ship too.
    assert held.get("family_education.yaml", 0) > 0
    assert held.get("financial.yaml", 0) > 0


def test_mixed_stores_are_marked() -> None:
    assert len({key for _, key in MIXED_STORES}) >= 12
    for core, key in MIXED_STORES:
        hit = dictionary.lookup(core)
        assert hit is not None, core
        assert hit.merchant.key == key, core
        assert hit.merchant.mixed is True, core
    # A shop with one trade is not a general store.
    for core in ("netflix", "kroger", "chevron", "olive garden"):
        hit = dictionary.lookup(core)
        assert hit is not None, core
        assert hit.merchant.mixed is False, core


def test_sub_brands_win_over_the_brand() -> None:
    assert len(SUB_BRANDS) >= 10
    for brand_core, brand_key, sub_core, sub_key, sub_category in SUB_BRANDS:
        brand = dictionary.lookup(brand_core)
        assert brand is not None, brand_core
        assert brand.merchant.key == brand_key, brand_core
        for core in (sub_core, f"{sub_core} 0412"):
            sub = dictionary.lookup(core)
            assert sub is not None, core
            assert sub.merchant.key == sub_key, core
            assert sub.merchant.category == sub_category, core
        assert brand.merchant.category != sub_category, sub_core
        # The sub-brand's words alone, in another order, are not the sub-brand.
        tail = sub_core.split()[-1]
        other = dictionary.lookup(f"{tail} {brand_core}")
        assert other is None or other.merchant.key != sub_key, sub_core


def test_no_entry_for_p2p_or_card_issuers() -> None:
    for core in NOT_MERCHANTS:
        assert dictionary.lookup(core) is None, core
    # Names that share a first word with one of them are still found, as themselves.
    for core, key in (
        ("citi bike nyc", "citi bike"),
        ("american airlines", "american airlines"),
        ("capital grille", "capital grille"),
    ):
        hit = dictionary.lookup(core)
        assert hit is not None, core
        assert hit.merchant.key == key, core
    # Money moved is recognised from the descriptor's kind, never from a merchant.
    moved = sorted(
        key
        for key, merchant in dictionary.merchants().items()
        if merchant.category.partition(".")[0] in ("transfers", "excluded")
    )
    assert moved == []
    # Who pays money in is a payroll processor or a benefits office, and says so.
    for key, merchant in dictionary.merchants().items():
        assert merchant.inflow == (merchant.category.partition(".")[0] == "income"), key
    assert dictionary.merchants()["adp"].inflow is True
    assert dictionary.merchants()["adp"].category == "income.salary"


# --- what the shipped files hold: subscriptions, bills and keywords -------------

MERCHANTS_MIN, MERCHANTS_MAX = 800, 1500
KEYWORDS_MIN, KEYWORDS_MAX = 100, 150
SUBSCRIPTIONS_MIN = 150
BILLS_MIN = 60

#: A biller that fronts many products, by a core a statement gives it and its key.
OPAQUE_BILLERS = (
    ("apple com bill", "apple services"),
    ("apl itunes com bill", "apple services"),
    ("itunes com bill", "apple services"),
    ("google play", "google play"),
    ("google lanternapp", "google play"),
    ("microsoft", "microsoft"),
    ("msft", "microsoft"),
)

#: What stands next to an opaque biller and is not one: ``(core, key, category)``.
BESIDE_OPAQUE_BILLERS = (
    ("apple store r123", "apple store", "shopping.electronics"),
    ("google fi", "google fi", "utilities.phone"),
    ("google youtube tv", "youtube tv", "leisure.subscriptions"),
    ("google google one", "google one", "leisure.software"),
    ("google fiber", "google fiber", "utilities.internet"),
    ("microsoft store", "microsoft store", "shopping.electronics"),
    ("microsoft xbox game pass", "xbox game pass", "leisure.games"),
)

#: A series the dictionary must know: ``(core, key, series, category)``.
KNOWN_SERIES = (
    ("spotify", "spotify", "subscription", "leisure.subscriptions"),
    ("nytimes nytimes disc", "new york times", "subscription", "leisure.news"),
    ("adobe creative cloud", "adobe", "subscription", "leisure.software"),
    ("dropbox", "dropbox", "subscription", "leisure.software"),
    ("playstation plus", "playstation plus", "subscription", "leisure.games"),
    ("planet fit club fees", "planet fitness", "subscription", "health.fitness"),
    ("hellofresh", "hellofresh", "subscription", "food.groceries"),
    ("dollar shave club", "dollar shave club", "subscription", "family.personal_care"),
    ("vzwrlss apocc visb", "verizon wireless", "bill", "utilities.phone"),
    ("t mobile auto pay", "t mobile", "bill", "utilities.phone"),
    ("comcast cable comm", "xfinity", "bill", "utilities.internet"),
    ("duke energy carolinas", "duke energy", "bill", "utilities.electricity"),
    ("pg e webrecurring", "pacific gas and electric", "bill", "utilities.electricity"),
    ("socalgas", "socalgas", "bill", "utilities.heating"),
    ("geico auto", "geico", "bill", "transport.car_insurance"),
    ("state farm insurance", "state farm", "bill", "transport.car_insurance"),
    ("simplisafe", "simplisafe", "bill", "utilities.other"),
    ("public storage", "public storage", "bill", "housing.fees"),
)

#: A billing name that covers several lines of one company, and one that
#: covers a single line: ``(core, key, mixed)``.
MIXED_BILLERS = (
    ("verizon recurring pay", "verizon", True),
    ("verizon wireless", "verizon wireless", False),
    ("verizon fios", "verizon fios", False),
    ("att bill payment", "at and t", True),
    ("at t mobility", "at and t wireless", False),
    ("state farm ro 27", "state farm", True),
    ("allstate", "allstate", True),
    ("geico auto", "geico", False),
    ("aetna", "aetna", False),
)

#: Names that are also a bank, a surname, a shop or an everyday word: only a
#: form that is the service's own may match, so these cores name nobody (or
#: somebody else, given as the key).
NOT_A_SERIES: tuple[tuple[str, str | None], ...] = (
    ("usaa", None),  # also a bank
    ("usaa credit card payment", None),
    ("nationwide", None),  # also a bank
    ("norton", None),  # a surname
    ("pandora jewelry", "pandora jewelry"),  # the jeweller, not the radio
    ("pandora", None),
    ("calm waters spa", None),
    ("frontier airlines", "frontier airlines"),  # not the telephone company
    ("spectrum", None),  # a word many businesses carry
    ("ring", None),
    ("equinox", None),
    ("zoom", None),
    ("cox", None),  # a surname
    ("clear", None),
    ("medium", None),
    ("tidal", None),
    ("aaa locksmith", None),
    ("apple", None),
)

#: A word and the category it says: found in an invented payee's core.
TELLING_WORDS = (
    ("harbor pizza", "pizza", "food.restaurants"),
    ("maple grill", "grill", "food.restaurants"),
    ("lantern cafe", "cafe", "food.coffee"),
    ("corner pharmacy", "pharmacy", "health.pharmacy"),
    ("maple family dental", "dental", "health.dental"),
    ("harbor vet clinic", "vet", "family.pets"),
    ("lantern fuel stop", "fuel", "transport.fuel"),
    ("harbor street parking", "parking", "transport.parking"),
    ("maple liquor", "liquor", "food.alcohol"),
    ("lantern daycare", "daycare", "family.childcare"),
    ("maple car wash", "car wash", "transport.car_maintenance"),
    ("harbor animal hospital", "animal hospital", "family.pets"),
)

#: Words that are a surname, a place or say too little: never a keyword.
NOT_KEYWORDS = (
    "baker",
    "cook",
    "butcher",
    "park",
    "bar",
    "gas",
    "market",
    "store",
    "shop",
    "club",
    "house",
    "garden",
    "auto",
    "home",
    "service",
    "services",
    "center",
    "bill",
    "payment",
    "online",
    "plus",
    "king",
    "hill",
    "springs",
)


def _series_counts() -> dict[str, int]:
    counts = {"subscription": 0, "bill": 0}
    for merchant in dictionary.merchants().values():
        if merchant.series is not None:
            counts[merchant.series] += 1
    return counts


def test_size_for_1_0() -> None:
    assert MERCHANTS_MIN <= len(dictionary.merchants()) <= MERCHANTS_MAX
    words = [entry for entry in _shipped_keywords() if "word" in entry]
    assert KEYWORDS_MIN <= len(words) <= KEYWORDS_MAX, len(words)
    assert len({entry["word"] for entry in words}) == len(words), "a keyword is written twice"
    # The sector files of this edition ship.
    held = {file for file, _ in _shipped_merchants()}
    for file in (
        "software.yaml",
        "news_music_games.yaml",
        "memberships_boxes.yaml",
        "fitness.yaml",
        "telecom_utilities.yaml",
        "insurance.yaml",
        "streaming.yaml",
    ):
        assert file in held, file


def test_series_floors() -> None:
    counts = _series_counts()
    assert counts["subscription"] >= SUBSCRIPTIONS_MIN, counts
    assert counts["bill"] >= BILLS_MIN, counts
    for core, key, series, category in KNOWN_SERIES:
        hit = dictionary.lookup(core)
        assert hit is not None, core
        assert hit.merchant.key == key, core
        assert hit.merchant.series == series, core
        assert hit.merchant.category == category, core
    for core, expected in NOT_A_SERIES:
        hit = dictionary.lookup(core)
        assert (hit.merchant.key if hit is not None else None) == expected, core
    # One billing name for several lines is a bill whose category is only suggested.
    for core, key, mixed in MIXED_BILLERS:
        hit = dictionary.lookup(core)
        assert hit is not None, core
        assert hit.merchant.key == key, core
        assert hit.merchant.series == "bill", core
        assert hit.merchant.mixed is mixed, core
    # A habit is not a series, whatever the shop.
    for key in ("kroger", "starbucks", "chevron", "walmart", "mcdonalds"):
        assert dictionary.merchants()[key].series is None, key


def test_every_series_has_a_cadence() -> None:
    without = sorted(
        key
        for key, merchant in dictionary.merchants().items()
        if merchant.series is not None and not merchant.cadences
    )
    assert without == []
    # A bill comes monthly or less often; nobody is billed for power by the week.
    hasty = sorted(
        key
        for key, merchant in dictionary.merchants().items()
        if merchant.series == "bill" and {"weekly", "biweekly"} & set(merchant.cadences)
    )
    assert hasty == []


def test_opaque_billers_are_mixed() -> None:
    assert len({key for _, key in OPAQUE_BILLERS}) >= 3
    for core, key in OPAQUE_BILLERS:
        hit = dictionary.lookup(core)
        assert hit is not None, core
        assert hit.merchant.key == key, core
        assert hit.merchant.mixed is True, core
        assert hit.merchant.series == "subscription", core
        assert hit.merchant.category == "leisure.software", core
    # What it fronts and the statement names, or the maker's own shop, is itself.
    for core, key, category in BESIDE_OPAQUE_BILLERS:
        hit = dictionary.lookup(core)
        assert hit is not None, core
        assert hit.merchant.key == key, core
        assert hit.merchant.category == category, core
        assert hit.merchant.mixed is False, core


def test_keywords_are_not_merchant_tokens() -> None:
    keywords = {
        entry["word"]: entry["category"] for entry in _shipped_keywords() if "word" in entry
    }
    owners: dict[str, list[tuple[str, str]]] = {}
    for _, entry in _shipped_merchants():
        for match in entry["match"]:
            plain = _plain(match)
            assert plain not in keywords, f"{entry['key']!r}: the match {plain!r} is a keyword"
            owners.setdefault(plain.split()[0], []).append((entry["key"], entry["category"]))
    # A word that opens a merchant's name ("pizza" in "pizza hut") may be a
    # keyword only when it files a payee in that merchant's group: a line of
    # that merchant cut short is then never suggested somewhere else.
    for word, category in keywords.items():
        group = category.partition(".")[0]
        elsewhere = sorted(
            key for key, held in owners.get(word, []) if held.partition(".")[0] != group
        )
        assert elsewhere == [], f"the keyword {word!r} ({category}) opens the name of {elsewhere}"
    # A merchant's own name is answered by the merchant, never by a word in it.
    for key in ("pizza hut", "taco bell", "burger king"):
        if key in dictionary.merchants():
            hit = dictionary.lookup(key)
            assert hit is not None, key
            assert hit.merchant.key == key


def test_keywords_say_what_was_bought() -> None:
    for core, word, category in TELLING_WORDS:
        assert dictionary.keyword(core, None) == KeywordHit(
            category=category, word=word, strong=False
        ), core
    words = {entry["word"] for entry in _shipped_keywords() if "word" in entry}
    assert sorted(words & set(NOT_KEYWORDS)) == []
    for word in NOT_KEYWORDS:
        assert dictionary.keyword(f"lantern {word}", None) is None, word
    # Only a processor that fronts one trade makes a keyword sure enough to state.
    strong = [entry for entry in _shipped_keywords() if entry.get("strong")]
    assert all("via" in entry for entry in strong), strong
    assert all(entry.get("via") is None or "word" not in entry for entry in strong), strong


def test_every_merchant_finds_itself() -> None:
    served = dictionary.merchants()
    lost: list[str] = []
    for _, entry in _shipped_merchants():
        for match in entry["match"]:
            hit = dictionary.lookup(_plain(match))
            if hit is None or hit.merchant is not served[entry["key"]] or hit.how != "exact":
                found = hit.merchant.key if hit is not None else None
                lost.append(f"{entry['key']!r}: {_plain(match)!r} finds {found!r}")
    assert lost == []


# --- the matcher, on tables built here ---------------------------------------

STREAMBOX: dict[str, Any] = {
    "key": "streambox",
    "name": "StreamBox",
    "category": "leisure.subscriptions",
    "match": ["streambox"],
    "series": "subscription",
    "cadences": ["monthly", "annual"],
}
STREAMBOX_KIDS: dict[str, Any] = {
    "key": "streambox kids",
    "name": "StreamBox Kids",
    "category": "family.childcare",
    "match": ["streambox kids"],
}
EVERYMART: dict[str, Any] = {
    "key": "everymart",
    "name": "EveryMart",
    "category": "shopping.general",
    "match": ["everymart", "evrymrt ~"],
    "mixed": True,
}
LANTERN: dict[str, Any] = {
    "key": "lantern books",
    "name": "Lantern Books",
    "category": "leisure.news",
    "match": ["lantern books"],
}
PAYWORKS: dict[str, Any] = {
    "key": "payworks",
    "name": "PayWorks",
    "category": "income.salary",
    "match": ["payworks"],
    "inflow": True,
}


def _table(
    files: Mapping[str, list[dict[str, Any]]], keywords: list[dict[str, Any]] | None = None
) -> Table:
    return dictionary.build(
        {name: {"merchants": entries} for name, entries in files.items()},
        {"keywords": keywords or []},
        version=1,
    )


def test_a_table_holds_what_the_entries_say() -> None:
    table = _table({"a.yaml": [STREAMBOX, EVERYMART, PAYWORKS]})
    assert table.merchants["streambox"] == Merchant(
        key="streambox",
        name="StreamBox",
        category="leisure.subscriptions",
        series="subscription",
        cadences=("monthly", "annual"),
    )
    assert table.merchants["everymart"].mixed is True
    assert table.merchants["everymart"].inflow is False
    assert table.merchants["payworks"].inflow is True
    assert table.version == 1


def test_the_longer_match_wins() -> None:
    table = _table({"a.yaml": [STREAMBOX], "b.yaml": [STREAMBOX_KIDS]})
    brand, kids = table.merchants["streambox"], table.merchants["streambox kids"]
    assert table.lookup("streambox") == Hit(brand, "exact")
    assert table.lookup("streambox monthly") == Hit(brand, "prefix")
    assert table.lookup("streambox kids") == Hit(kids, "exact")
    assert table.lookup("streambox kids annual") == Hit(kids, "prefix")
    # The order of the files and of the entries does not decide it.
    swapped = _table({"a.yaml": [STREAMBOX_KIDS, STREAMBOX]})
    assert swapped.lookup("streambox kids annual") == Hit(kids, "prefix")


def test_two_merchants_equally_long_is_no_match() -> None:
    harbor = {"key": "harbor fuel", "name": "Harbor Fuel", "category": "transport.fuel"}
    market = {"key": "corner market", "name": "Corner Market", "category": "food.groceries"}
    table = _table(
        {
            "a.yaml": [
                {**harbor, "match": ["harbor fuel", "harbor ~"]},
                {**market, "match": ["corner market ~", "market ~"]},
            ]
        }
    )
    assert table.lookup("harbor fuel corner market") is None  # two tokens each
    assert table.lookup("harbor market") is None  # one token each
    # One of them alone, or one of them longer, is a match.
    assert table.lookup("harbor fuel market") == Hit(table.merchants["harbor fuel"], "prefix")
    assert table.lookup("the corner market") == Hit(table.merchants["corner market"], "anywhere")
    # Two matches of one merchant are one answer, told by the better of them.
    both = _table({"a.yaml": [{**harbor, "match": ["harbor", "fuel ~"]}]})
    assert both.lookup("harbor fuel") == Hit(both.merchants["harbor fuel"], "prefix")


def test_anywhere_needs_the_marker() -> None:
    table = _table({"a.yaml": [STREAMBOX, EVERYMART]})
    everymart = table.merchants["everymart"]
    # "streambox" has no marker: only at the start of the core.
    assert table.lookup("pay streambox") is None
    assert table.lookup("everymart") == Hit(everymart, "exact")
    assert table.lookup("online everymart") is None
    # "evrymrt ~" has it: anywhere, and said so.
    assert table.lookup("online evrymrt 0042") == Hit(everymart, "anywhere")
    assert table.lookup("evrymrt 0042") == Hit(everymart, "prefix")
    assert table.lookup("evrymrt") == Hit(everymart, "exact")
    assert table.lookup("online evrymrtx") is None


def test_keyword_and_via_keyword() -> None:
    assert dictionary.keyword("harbor pizza", None) == KeywordHit(
        category="food.restaurants", word="pizza", strong=False
    )
    assert dictionary.keyword("harbor pizzas", None) is None  # whole tokens
    assert dictionary.keyword("harbor lantern", None) is None
    # Behind Toast everything is a restaurant, and that is sure enough to state.
    for core in ("harbor lantern", "harbor pizza", "maple"):
        assert dictionary.keyword(core, "toast") == KeywordHit(
            category="food.restaurants", word="toast", strong=True
        )
    # The word "toast" in a name is not the processor, and another processor is not Toast.
    assert dictionary.keyword("maple toast", None) is None
    assert dictionary.keyword("harbor lantern", "square") is None
    assert dictionary.keyword("harbor pizza", "square") == KeywordHit(
        category="food.restaurants", word="pizza", strong=False
    )


def test_keywords_are_read_in_file_order_and_a_via_keyword_first() -> None:
    table = _table(
        {"a.yaml": [STREAMBOX]},
        [
            {"word": "pet supply", "category": "family.pets"},
            {"word": "grill", "category": "food.restaurants"},
            {"word": "supply", "category": "shopping.household"},
            {"word": "pizza", "category": "food.fast_food", "via": "square"},
            {"word": "pizza", "category": "food.restaurants"},
            {"word": "vet", "category": "family.pets", "strong": True},
        ],
    )
    assert table.keyword("maple pet supply", None) == KeywordHit("family.pets", "pet supply", False)
    assert table.keyword("maple supply", None) == KeywordHit("shopping.household", "supply", False)
    # Two tokens are found only side by side.
    assert table.keyword("pet and supply", None) == KeywordHit(
        "shopping.household", "supply", False
    )
    # The first in the file wins, wherever it stands in the core.
    assert table.keyword("supply grill", None) == KeywordHit("food.restaurants", "grill", False)
    assert table.keyword("harbor vet", None) == KeywordHit("family.pets", "vet", True)
    # A word tied to a processor applies behind it only, and before the others.
    assert table.keyword("harbor pizza", "square") == KeywordHit("food.fast_food", "pizza", False)
    assert table.keyword("harbor pizza", None) == KeywordHit("food.restaurants", "pizza", False)
    assert table.keyword("harbor pizza", "toast") == KeywordHit("food.restaurants", "pizza", False)
    assert table.keyword("harbor grill", "square") == KeywordHit("food.restaurants", "grill", False)
    assert table.keyword("", None) is None


# --- the loader refuses what is malformed ------------------------------------


def test_yaml_is_loaded_safely() -> None:
    tagged = "merchants:\n  - !!python/object/apply:os.getcwd []\n"
    with pytest.raises(DictionaryError, match=r"hostile\.yaml"):
        dictionary.parse("hostile.yaml", tagged)
    with pytest.raises(DictionaryError, match=r"hostile\.yaml"):
        dictionary.parse("hostile.yaml", "merchants: !!python/name:os.getcwd\n")
    with pytest.raises(DictionaryError, match=r"broken\.yaml"):
        dictionary.parse("broken.yaml", "merchants: [unclosed\n")
    assert dictionary.parse("fine.yaml", "merchants:\n  - key: streambox\n") == {
        "merchants": [{"key": "streambox"}]
    }


def _bad(change: dict[str, Any], *, drop: str | None = None) -> dict[str, Any]:
    entry = {**STREAMBOX, **change}
    if drop is not None:
        del entry[drop]
    return entry


@pytest.mark.parametrize(
    ("entry", "problem"),
    [
        (_bad({"key": "Streambox"}), "key"),
        (_bad({"key": "stream-box"}), "key"),
        (_bad({"key": " streambox"}), "key"),
        (_bad({"key": "s" * 65}), "key"),
        (_bad({"key": 42}), "key"),
        (_bad({}, drop="name"), "name"),
        (_bad({"name": "  "}), "name"),
        (_bad({"category": "leisure.streaming"}), "category"),
        (_bad({}, drop="match"), "match"),
        (_bad({"match": []}), "match"),
        (_bad({"match": "streambox"}), "match"),
        (_bad({"match": ["StreamBox"]}), "match"),
        (_bad({"match": ["streambox~"]}), "match"),
        (_bad({"match": ["~"]}), "match"),
        (_bad({"match": ["streambox ~ com"]}), "match"),
        (_bad({"match": ["streambox", "streambox ~"]}), "match"),
        (_bad({"series": "habit"}), "series"),
        (_bad({"cadences": ["daily"]}), "cadences"),
        (_bad({"cadences": ["monthly", "monthly"]}), "cadences"),
        (_bad({"cadences": "monthly"}), "cadences"),
        (_bad({"cadences": ["monthly"]}, drop="series"), "cadences"),
        (_bad({"mixed": "yes"}), "mixed"),
        (_bad({"inflow": 1}), "inflow"),
        (_bad({"logo": "streambox.png"}), "logo"),
    ],
)
def test_a_malformed_merchant_is_refused_with_its_file_and_entry_named(
    entry: dict[str, Any], problem: str
) -> None:
    with pytest.raises(DictionaryError) as raised:
        _table({"a.yaml": [EVERYMART], "odd.yaml": [LANTERN, entry]})
    message = str(raised.value)
    assert "odd.yaml" in message
    assert "entry 2" in message
    assert problem in message


def test_a_key_or_a_match_used_twice_is_refused() -> None:
    twin = {**LANTERN, "match": ["lantern"]}
    with pytest.raises(DictionaryError, match=r"b\.yaml: entry 1 .*lantern books.*a\.yaml"):
        _table({"a.yaml": [LANTERN], "b.yaml": [twin]})
    other = {**LANTERN, "key": "lantern used books", "match": ["lantern books ~"]}
    with pytest.raises(DictionaryError, match=r"b\.yaml: entry 1 .*lantern books"):
        _table({"a.yaml": [LANTERN], "b.yaml": [other]})
    # A merchant may not be matched by a word that is a keyword.
    pizza = {**LANTERN, "key": "pizza", "match": ["pizza"]}
    with pytest.raises(DictionaryError, match=r"keywords\.yaml: entry 1 .*a\.yaml: entry 1"):
        _table({"a.yaml": [pizza]}, [{"word": "pizza", "category": "food.restaurants"}])


@pytest.mark.parametrize(
    ("entry", "problem"),
    [
        ({"word": "Pizza", "category": "food.restaurants"}, "word"),
        ({"word": "wood fired pizza", "category": "food.restaurants"}, "word"),
        ({"category": "food.restaurants"}, "word"),
        ({"word": "pizza"}, "category"),
        ({"word": "pizza", "category": "food.pizza"}, "category"),
        ({"word": "pizza", "category": "food.restaurants", "strong": "very"}, "strong"),
        ({"word": "pizza", "category": "food.restaurants", "via": "Toast"}, "via"),
        ({"word": "pizza", "category": "food.restaurants", "weight": 3}, "weight"),
    ],
)
def test_a_malformed_keyword_is_refused(entry: dict[str, Any], problem: str) -> None:
    good = {"word": "grill", "category": "food.restaurants"}
    with pytest.raises(DictionaryError) as raised:
        _table({"a.yaml": [STREAMBOX]}, [good, entry])
    message = str(raised.value)
    assert "keywords.yaml" in message
    assert "entry 2" in message
    assert problem in message


@pytest.mark.parametrize(
    "document",
    [None, [], {"merchant": []}, {"merchants": {}}, {"merchants": []}, {"merchants": ["netflix"]}],
)
def test_a_file_of_another_shape_is_refused(document: object) -> None:
    with pytest.raises(DictionaryError, match=r"odd\.yaml"):
        dictionary.build({"odd.yaml": document}, {"keywords": []}, version=1)


def test_a_version_must_be_a_positive_whole_number() -> None:
    versions: tuple[Any, ...] = (0, -1, True, "1", 1.5)
    for version in versions:
        with pytest.raises(DictionaryError, match="version"):
            dictionary.build(
                {"a.yaml": {"merchants": [STREAMBOX]}}, {"keywords": []}, version=version
            )


# --- speed ---------------------------------------------------------------------


def test_lookup_is_fast() -> None:
    cores = (
        "netflix com",
        "lantern books springfield",
        "google youtube tv",
        "harbor pizza",
        "corner grocer 0412",
        "a b c d e f",
        "hulu",
        "quiet bean coffee",
        "streambox monthly plan",
        "",
    )
    dictionary.lookup("netflix")  # loaded before the clock starts
    started = time.perf_counter()
    for index in range(10_000):
        dictionary.lookup(cores[index % len(cores)])
    assert time.perf_counter() - started < 1.0
