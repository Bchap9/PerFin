# The merchant dictionary

Static data that ships inside the PerFin package and is read on the host.
`app.domain.categorisation.dictionary` loads it once per process; looking a
merchant up makes no request to anything.

| File | Holds |
| --- | --- |
| `meta.yaml` | `version` (a whole number) and `digest` (SHA-256 of the entries) |
| `merchants/<sector>.yaml` | `merchants:`, a list of merchant entries |
| `keywords.yaml` | `keywords:`, a list of keyword entries, read in order |

Processor prefixes (`SQ *`, `TST*`) and the wording that tells a transfer
from a purchase are not here: they are code, in `../cleanup.py`.

## A merchant entry

```yaml
- key: netflix                    # the merchant key stored on a transaction
  name: Netflix                   # what a person sees
  category: leisure.subscriptions # a default category's key
  match: [netflix]                # how a cleaned descriptor names it
  series: subscription            # optional: subscription or bill
  cadences: [monthly]             # optional, with `series`: how it usually bills
  mixed: true                     # optional: a store that sells everything
  inflow: true                    # optional: pays money in (a payroll processor)
```

- `key` and every `match` are in merchant-key form: lower-case `a-z0-9`
  tokens joined by single spaces, at most 64 characters. A `key` is unique
  across all files. Once shipped it does not change: transactions, learned
  decisions and rules refer to it.
- `match` is compared with the **core** of a descriptor (what clean-up leaves:
  `POS DEBIT NETFLIX.COM 866-579-7172 CA` becomes `netflix`), by whole tokens
  and nothing else. There are no wildcards and no regular expressions.
  - A plain match must stand at the start of the core: `netflix` finds
    `netflix` and `netflix com`, not `netflixx` and not `pay netflix`.
  - A match that ends in ` ~` (a space and a tilde) is found anywhere in the
    core: `youtube tv ~` finds `google youtube tv`. Use it only for a name no
    other business carries, because it is looked for in every descriptor.
  - A match that ends in ` =` (a space and an equals sign) must be the whole
    core: `wendy =` finds `wendy` (what `WENDY'S 0412 AUSTIN TX` cleans to)
    and not `wendy flowers`. Clean-up has already dropped store numbers,
    places, web endings and company suffixes, so a chain's own line usually
    is the bare name.
  - The match with the most tokens wins, so a sub-brand is its own entry with
    a longer match (`streambox kids` beats `streambox`). When the longest
    matches belong to two merchants, nobody is found.
  - The same tokens may belong to one merchant only, and may not be a keyword
    or the letters of a processor prefix.
- Write one `match` for each form a statement prints, the squashed ones too
  (`disney plus`, `disneyplus`). Punctuation is already gone from a core:
  `.`, `*` and `'` become spaces, and a `+` that ends a name is read as the
  word, so `AMC+` arrives as `amc plus`.
- If the name is an everyday word or is shared with other businesses, match
  only a form that cannot be anyone else, or leave the merchant out. A wrong
  category stated as a fact is worse than no answer.
- A one-token match that is a word, a surname, a place or a possessive stem
  (`subway`, `mcdonald`, `hilton`, `wendy`, `adobe`) is written with ` =`,
  because as a prefix it would also claim "Subway Tile and Stone",
  "McDonald Family Dental" and "Hilton Head Plumbing", give them the chain's
  category as a fact and its key, and let one correction on the stranger
  teach the whole chain. Beside it, write the two-token forms the chain's
  statements really carry (`hilton garden`, `adobe creative`, `domino
  pizza`). A coined name (`netflix`, `chipotle`) stays a plain prefix. Every
  such name gets a line in `NOT_THE_CHAIN` in the unit test.
- A payment to a store's credit card is not shopping there, and it has no
  entry. The matcher also refuses a merchant when the rest of the core says
  "credit card", "credit services" or "card services", or says "payment"
  (`pmt`, `pymt`, `epay`, `autopay`) after a merchant without a `series`:
  `KOHLS PAYMENT` and `HOME DEPOT CREDIT SERVICES` name nobody, while
  `GEICO PAYMENT` is still the bill.
- `category` is a key of the default set
  (`app.domain.transactions.categories`), not a name: a household may have
  renamed the category.
- `series` says the merchant's charges are usually a subscription or a bill;
  detection uses it as a hint. A `subscription` is a fixed price for a
  service (streaming, software, a gym, a box); a `bill` is what a household
  owes whatever it does (phone, internet, power, gas, water, an insurer, an
  alarm, a storage unit). A shop where people only often buy is neither.
  A `mixed` entry without a `series` is read by detection as a general
  store: its uncategorised charges are a habit, and only one price to the
  cent at a regular rhythm is suggested as a series. Give a `mixed` entry a
  `series` only when nearly everything billed under that name is a plan (an
  app store), because a known series is suggested after a single charge.
  Every entry with a `series` names its `cadences`, from `weekly`,
  `biweekly`, `monthly`, `two_monthly`, `quarterly`, `half_yearly`, `annual`.
- `mixed: true` makes the category a suggestion only. It is for a general
  store, for an opaque biller that fronts many products under one name (an
  app store: one entry, `series: subscription`, `leisure.software`), and for
  a company whose one billing name covers several lines (an insurer that
  sells car, home and life cover; a carrier's bare name). A product the
  statement does name is its own entry with a longer match.
- An order through a delivery or ticket platform is the platform's:
  `engine.PLATFORMS` (DoorDash, Grubhub, Instacart, Eventbrite) names the
  processors whose `via` is the key of their own entry here, and a line
  behind one of them takes that entry's key and category whatever merchant
  it names. Such a platform needs an entry of its own; its membership is a
  separate entry with a `series`, which is left as it is.
- `inflow: true` is for a company that pays a person (a payroll processor);
  it answers for money coming in only.
- No other field is read; an unknown field is an error.

## A keyword entry

```yaml
- word: pizza                 # one or two tokens, found as whole tokens
  category: food.restaurants
  strong: true                # optional: sure enough to state as a fact
  via: toast                  # optional: only behind this processor
```

A keyword answers for a payee no merchant entry knows. The entries are read in
file order and the first word found in the core answers, so put the more
telling word first. An entry with `via` applies only to a charge that came
through that processor (the name clean-up gives it), and is read before the
others; with `via` and no `word` it covers everything the processor fronts.
A keyword is a word that says what was bought. A surname or a place is not
one, nor is a word that fits any business (`market`, `shop`, `club`). A word
that opens a listed merchant's name (`pizza` in Pizza Hut) may be a keyword
only in that merchant's group of categories. Only an entry with `via` and no
`word` may be `strong`. A `strong` entry still gives way to a word in the
line that names another category (`TST* NORTHSIDE ESPRESSO BAR` is coffee,
as a suggestion): PerFin does not state what the line itself contradicts.
A keyword in an `income.` category (`wages`, `dividend earned`) answers
money coming in only (`engine.INFLOW_ONLY_GROUPS`). A word that is only
the bank's own wording about itself (a fee, interest) is not a keyword: it
is a kind, read by `../cleanup.py`, and stated.

## Sources

The files are our own work under AGPL-3.0-or-later.

Allowed inputs: what the authors know about brand names and the descriptor
shapes banks print; `osmlab/name-suggestion-index` (BSD-3-Clause, notice kept
in the README); Wikidata (CC0); `greggles/mcc-codes` (Unlicense) and the IRS
list (public domain) as reference.

Not allowed: Plaid, MX, Yodlee, Ntropy or any enrichment product's data or
descriptions; the Visa and Mastercard manuals and ISO 18245; OpenStreetMap
extracts (share-alike); data sets of unclear origin; another product's
database, scraped or exported; any line of a real person's statement or of
the scratch corpus.

Brand names are used to name the merchant, nothing more.

Every entry in these files so far was written by hand from the first allowed
input (what the authors know). No line was taken from
`osmlab/name-suggestion-index`, so its notice is not reproduced here. Whoever
first takes a line from it adds the project's BSD-3-Clause copyright notice,
conditions and disclaimer to this section in the same change.

## Adding or changing a merchant

1. Edit the sector file under `merchants/` (or `keywords.yaml`). Keep the
   SPDX comment on the first line of every file.
2. Add one to `version` in `meta.yaml`.
3. From `backend/`, run
   `uv run pytest -q tests/unit/test_merchant_dictionary.py`. The digest test
   fails and prints the new digest; put it in `meta.yaml` and run again.
4. Open a pull request. The test is the gate: the schema, the token form,
   unique keys, a match owned by one merchant, every match finding its own
   merchant, every category a default key, no match that is a keyword or a
   processor prefix, the names other businesses carry (`NOT_THE_CHAIN`) and
   the store-card payments (`STORE_CARD_PAYMENTS`) naming nobody, a cadence for every series, the size (800 to 1,500
   merchants, 100 to 150 keywords), and the digest pinned to the version.

The version is what makes installed households catch up: each household
records the version it was last evaluated with, and a newer one queues a
refresh that re-evaluates only the rows nobody decided. A changed entry
without a new version would reach new rows and leave old guesses as they were,
which is why the digest and the version must move together.
