# Categories and subscriptions

How PerFin decides what a transaction was for (food, fuel, entertainment),
how it learns from what you correct, and how it finds the subscriptions and
bills you pay. Everything on this page happens on your own host: there is no
hosted model, no lookup service and no request to anything
([what leaves the host](security/what-leaves-the-host.md)).

Code: `backend/app/domain/categorisation/` (clean-up, the merchant list, what
was learned, the engine), `backend/app/domain/rules/` (rules),
`backend/app/domain/recurring/` (subscriptions and bills). Tables and
columns: [DATA_MODEL.md](DATA_MODEL.md) ("Categorisation and
subscriptions"). Endpoints: [API.md](API.md) ("Categorisation notes",
"Subscription notes").

## The short version

- A new household's first import is mostly categorised with no set-up, from
  a list of merchants that ships inside PerFin.
- Every category says how it was decided. A guess PerFin is not sure of is
  shown as a **suggestion**: it is a question, it counts in no report and no
  budget until you accept it.
- What you chose is never changed by a guess. Not by a later import, not by
  a newer merchant list, not by anything PerFin works out by itself.
- Correct a merchant once and the next charge from it is filed the same way.
  "Always categorise ... as" makes that a rule in one step, with a preview
  and an exact undo.
- Subscriptions and bills have their own page. What you decide about one
  ("this is not a subscription") stays decided.

## How a category is decided

For each transaction, the first of these that has an answer wins:

| # | Who or what decided | Shown as | How sure |
| --- | --- | --- | --- |
| 1 | You: you picked a category, cleared it, accepted a suggestion or split the transaction | "You chose this" | certain |
| 2 | The file: a category column in a CSV, a category in a QIF file | "From the file" | 0.95 |
| 3 | One of your rules, in the order of the rules list | "Rule: *name*" | certain |
| 4 | The default category of the transaction's payee | "Payee default" | 0.95 |
| 5 | What PerFin learned from your corrections for this merchant | "Learned from you" | 0.80, 0.90 or 0.95 after one, two, or three and more decisions, times the share of your decisions that agree |
| 6 | The merchant list, or the kind of line (a card payment, an ATM withdrawal, a salary, interest, a bank fee) | "Merchant list" | 0.90 for a merchant; less in the cases below |
| 7 | A telling word in the description ("pizza", "pharmacy"), or a bill's word (rent, a utility, insurance, taxes, childcare, tuition) or pay's word for money in (`DES:WAGES`), in a labelled ACH line's entry description (`DES:TUITION`) when the name has none | "Keyword" | 0.60 |
| 8 | Nothing | no category | |

Categories a transaction had before PerFin recorded this (before
1.0.0-rc.10) are counted as "From before this version". They are treated
like your own choice as far as guessing goes: no guess ever replaces one.

**The 0.80 line.** Rows 5 to 7 are guesses. A guess of 0.80 or more is
written as the transaction's category, with its source and how sure PerFin
is, for example "Merchant list, 90%". A guess below 0.80 is only a
suggestion: the transaction stays uncategorised, the grid asks "*Category*?"
with **Accept** and a picker beside it, and "Accept suggestions" in the bulk
bar takes many at once. Accepting is your choice like any other (row 1).

A merchant-list answer is deliberately less sure, or only a suggestion,
when:

- the store sells everything (Amazon, Walmart, Target, Costco, an app store,
  an insurer whose one billing name covers car, home and life): always a
  suggestion, 0.60;
- the name was found in the middle of the line instead of at its start
  (0.85), or behind a payment processor such as Square or PayPal (0.05
  less);
- the money is coming **in** and the line is neither a refund nor a card
  purchase: it may be a refund, or earnings and payouts that are no spending
  at all, so it is a suggestion (0.70);
- your household files this merchant in several places (see "Learned"
  below): a suggestion (0.60 at most).

Money that is not spending is recognised from the wording of the line, not
from a merchant: a card payment, a transfer between your own accounts, a
loan payment and cash from an ATM go to the transfer categories, which
reports leave out of spending; a salary to "Salary", interest to interest
received or paid, a bank fee to "Bank fees". A wire transfer is only
suggested as a transfer between your accounts, because a wire may as well
pay somebody else. A cheque, a deposit and a
payment between people (Zelle, Venmo, Cash App) are **never** guessed: the
line does not say what the money was for.

Nothing is guessed for the lines of a split.

### Where to see it

- **Transactions** (`/transactions`): the category cell says who decided
  and, for a guess, how sure; a suggestion reads as a question
  ("Groceries?") with Accept. Two quick filters, "Suggestions" and "No
  category", carry their counts. The payee cell shows the merchant PerFin
  recognised with the bank's own wording underneath; the bank's wording is
  never changed or dropped. A row opens the drawer, whose "Bank wrote" and
  "Category by" lines say the same.
- **Inbox** (`/inbox`): one card with the merchants that have the most
  suggestions, each with "Accept N" (your choice for those rows), and a
  line with the number of transactions that have no category.
- **Settings, "What PerFin learned"** (`/settings/learned`): "How
  transactions got their category" counts your transactions by source,
  over the accounts you can see, and names the version and size of the
  merchant list.

## Learned from you

When you choose a category for a transaction that names a merchant, PerFin
counts it: one decision, for that merchant, for money out or money in. It
counts transactions, not clicks: changing your mind on the same transaction
moves the decision, it does not add one.

The next transaction from that merchant gets the category with the most
decisions (the most recent on a tie):

- one decision is enough to file the next charge as a fact (0.80); two and
  three make it surer;
- when fewer than 60% of your decisions for a merchant agree, you evidently
  file it in more than one place. PerFin then states nothing as a fact for
  that merchant, neither from what it learned nor from the merchant list:
  you get a suggestion at most;
- a decision older than three years is a suggestion.

What is learned is a plain table of counts (`merchant_memory`), at most
5,000 entries per household. It is not a model, it is the same every time,
and it survives deleting and re-importing a statement. After the upgrade to
rc.10 it starts from the categories your transactions already had, so years
of hand work count from the first day. Payments between people, transfers,
card payments, cash, cheques, fees and interest teach nothing.

**To see and forget it**: Settings, "What PerFin learned"
(`/settings/learned`), "Categories learned from you". Each entry shows the merchant, the category,
and how many of your decisions agree. **Forget** removes the entry (members
and above). Forgetting changes no transaction: categories already written
stay, and the next charge from that merchant is guessed without it. When you
were unanimous three times or more and no rule covers the merchant, the
entry offers "Always categorise ...".

**A shop the list does not know, printed with its town.** `JUNEBUG ASHGROVE
OH` may be the shop "Junebug" in Ashgrove or the shop "Junebug Ashgrove":
the line cannot say. PerFin keeps both words as the merchant, so two shops
that only begin alike ("The Hideout", "The Pantry") are never one merchant
and never share a correction, a rule or a series. One thing carries over:
when you have already filed the shop under its first word alone (from a line
such as `SQ *JUNEBUG`), that answers for the longer line too, unless the
first word is no name of its own (an article, a title, a colour, a
direction, a word for what is sold, or fewer than five letters). It does
not carry the other way: filing `JUNEBUG ASHGROVE OH` teaches that merchant
and no other, so `SQ *JUNEBUG` may need its own first correction.

## The merchant list

A list of merchants that ships inside PerFin: 1,427 United States merchants
in version 7 (national and large regional chains, utilities, insurers,
telecoms, and about 400 subscription services and billers), and 124
keywords. Merchants outside the United States are not covered in 1.0.

- **Where it lives**: plain YAML files in the image, under
  `backend/app/domain/categorisation/data/` (`meta.yaml`, one file per
  sector in `merchants/`, `keywords.yaml`). They are read once when a
  process starts. Nothing is downloaded, and looking a merchant up makes no
  request.
- **How it matches**: PerFin first cleans the bank's description down to who
  was paid: `POS DEBIT NETFLIX.COM 866-579-7172 CA` becomes `netflix`;
  `SQ *CORNER BAKERY 0417 AUSTIN TX` becomes `corner bakery`, paid through
  Square. Bank prefixes, card and reference numbers, dates, processor
  prefixes, phone numbers, web endings, cities, states and store numbers go;
  in an ACH line the company stays and the individual's name is dropped; for
  a payment between people the result is the service, never the other
  person. Then whole words are compared with the list: no wildcards and no
  regular expressions. The longest match wins (`amazon prime` beats
  `amazon`), and when two merchants match equally well, nobody is named.
- **An order through a platform is the platform's.** A line that came
  through DoorDash, Grubhub, Instacart or Eventbrite (`GRUBHUB*TACOBELL`,
  `DD *HARBOR PIZZA`) names the platform as the merchant and takes the
  platform's category (food delivery, groceries, events), whatever
  restaurant, shop or event stands behind it: that is who was paid and what
  the charge was. A rule on the restaurant therefore does not match its
  delivery orders. The platform's own membership (DashPass, Instacart+) stays
  its own merchant, and any other processor (Square, PayPal, Toast) only
  fronts the merchant it names.
- **Sources and licence**: the files are PerFin's own work under
  AGPL-3.0-or-later, written by hand from what the authors know about brand
  names and the shapes banks print. Brand names are used to name the
  merchant and for nothing else. No enrichment product's data, no card
  network manual, no scraped or exported database and no line of anybody's
  statement went into it. The full list of allowed and refused sources is in
  the data directory's `README.md`.
- **Proposing a merchant**: edit the sector file, add one to `version` in
  `meta.yaml`, run `uv run pytest -q tests/unit/test_merchant_dictionary.py`
  from `backend/` (it prints the new digest to put in `meta.yaml`) and open
  a pull request. That test is the review gate: the schema, unique keys, the
  word form, one owner per match, every category a default category, and a
  digest pinned to the version. The `README.md` beside the files explains
  every field, and when a name must be written so that it cannot claim
  another business ("Subway Tile and Stone" is not the sandwich chain). A
  wrong category stated as a fact is worse than no answer: when in doubt,
  mark a store `mixed` or leave it out.
- **Updates**: a new PerFin release may carry a newer list. Within an hour
  of the upgrade a background job looks again at your transactions, and
  **only at those nobody decided**: uncategorised ones, suggestions, and
  categories that came from the merchant list or a keyword. A category from
  you, from the file, from a rule, from a payee default, from what PerFin
  learned from you or from before this version is never touched, and
  neither is a split. Owners and admins can ask for the same pass at once
  with "Check again" on Settings, "What PerFin learned". While a household is
  behind, that card says "Not up to date yet". The pass is audited with
  counts only.

If you have replaced the default categories with your own tree, the merchant
list has less to offer: a merchant's category is found by its built-in key,
then by the key of its group, then not at all. Learning from your
corrections works with any tree.

## "Always categorise ... as"

In the transaction drawer (`/transactions?tx=<id>`), for members and
above: under the category, an **unticked** box, **Always use *category*
for *merchant***, shown at the moment you choose a category. The same is in
a transaction's row menu as **Always categorise *merchant* as...**, and the
Inbox offers it for a merchant you filed the same way three times ("Make it
a rule", which opens the drawer with the box ticked;
`/transactions?always=<id>`). Nothing is saved before Save.

- **Preview first.** Before anything is saved, the drawer shows what the
  server counted over your whole ledger, on the accounts you may edit: how
  many of the merchant's transactions have no category, how many carry a
  guess that would change, and how many somebody decided (those never
  change).
- **It makes an ordinary rule** with one condition (the merchant) and one
  action (set the category), placed last in your rules list. If the merchant
  already has such a rule, its category is changed instead of adding a
  second rule. If an earlier rule already decides this transaction, saving
  is refused and that rule is named, so you never save a rule that cannot
  take effect.
- **Existing transactions are a separate, unticked choice**: "Also change
  the existing *n* transactions". It changes uncategorised transactions and
  guesses, and never a category you, a file, a payee default, another rule
  or an earlier version decided.
- **Undo.** The drawer offers Undo right after saving a new rule, and
  Settings, Rules (`/settings/rules`) offers it later. Undo deletes the rule and returns exactly
  the transactions that rule categorised to "not decided"; PerFin then
  guesses for them again as it would have. Because every transaction
  records which rule set its category, this is exact at any later time. A
  member can undo a merchant rule; undoing any other rule needs an owner or
  admin.
- It cannot be made from a transaction that names no merchant, that is a
  transfer, a card payment or a payment to a person, or that is split; the
  drawer says which, in place of the box.

Re-applying your rules ("Apply rules") no longer changes a category you
chose yourself. It still tags, flags and annotates such a transaction.

## Subscriptions and bills

The **Subscriptions** page (Plan, Subscriptions: `/plan/subscriptions`)
lists what you pay for regularly: what it costs per month and per year, what
needs a look, what is due in the next 30 days, and what ended. What needs a
look is also on the Inbox, one card per series, and Home's "Coming up"
lists the next charges.

### What counts

PerFin looks at up to three years of transactions per account and groups
them by merchant. A group becomes a series when the charges come at a steady
rhythm (weekly, every two weeks, monthly, every two months, quarterly,
half-yearly or yearly) at a steady amount. A merchant the list knows as a
subscription or a bill needs fewer charges to be believed: two monthly
charges instead of three, and a single yearly charge is enough to suggest
one. Two plans at one merchant are two series.

Kept out on purpose:

- **Habits.** Groceries, restaurants, coffee, fuel, parking, taxis, transit,
  general shopping and cash come round every week without being a
  subscription. They are dropped, unless the amount is the same every time,
  every gap is regular and there are at least six charges, and then the
  series is only suggested.
- **Stores that sell everything.** Amazon, Walmart, Target, Costco and the
  other general stores of the merchant list are treated as a habit while
  their transactions have no category: a monthly order that happens to
  cost about the same is not a subscription. A plan billed through such a
  store (a reading or delivery membership) is still found: the same price
  to the cent at a regular rhythm is suggested from the third monthly
  charge, under "Needs a look", and counts in the cost once you confirm it.
  Give its transactions a category that is not a habit (Subscriptions, say)
  and it is read like any other series.
- **Money moving between your own accounts**: transfers, card payments,
  cash, cheques, fees, interest, and anything in a transfer or ignored
  category.
- A regular payment to a person (rent by Zelle, say) is a suggested bill at
  most: PerFin cannot tell rent from splitting dinner.
- The same withdrawal, cash deposit or ATM line every month, on lines
  that name nobody and that nobody gave a category, is only suggested: it
  may be pocket money or a payment to somebody, and only you know which.

### Bill or subscription

- A **bill** is what a household owes whatever it does: rent, mortgage,
  loans, insurance, power, gas, water, phone, internet, childcare, taxes.
  The amount may vary.
- A **subscription** is a fixed price for a service you chose: streaming,
  software, a gym, a news site, a box.
- Regular money in is **income**; anything else that repeats is "other".
  Both stay on Plan, Upcoming, "Recurring items" tab and are not called
  subscriptions.

"This is a bill" / "This is a subscription" corrects the kind.

### The states

| State | What it means | Counted in the cost |
| --- | --- | --- |
| Might be a subscription (or bill) | found, with thin evidence; listed under "Needs a look" | no |
| Found for you | found with enough evidence | yes |
| Confirmed | you said it is one | yes |
| Paused | you paused it | no |
| Ended | the charges stopped, or you said it ended | no |
| Not a subscription | you dismissed it | no |

**What you decide sticks.** Confirm, Not a subscription, Pause, Resume, End
and Reopen are recorded as yours, and detection never changes a state or a
kind you set. A dismissed series does not come back, also not when its price
changes (a different plan you start at the same merchant later is suggested
again, and stays a suggestion until you decide), and not when a later
merchant list or clean-up gives the merchant a new name: the series follows
the charges of its own account. When the charges of a series you dismissed
or ended now read as two merchants, what is found under either name on that
account is only suggested, without a "New" alert. There is one factual
exception: a confirmed series whose charges
really stop is marked ended, and PerFin tells you. Deleting a found series
from Plan, Upcoming now dismisses it instead of deleting it, which is why it
no longer reappears the next night. A series you typed in by hand is yours
alone: detection never touches it.

### Why nothing is "missed" before a statement is imported

PerFin has no bank feed. It knows what your statements say and nothing
after them. So "today" for an account is the date of its newest
transaction, not the date on the wall. A subscription is not "stopped"
because nobody has uploaded November yet: only transactions that arrive
after the expected date, without the charge among them, make it missed.

### What it reports

Each change is recorded once and listed under "Needs a look" until somebody
has seen it:

- **New**: a series whose first charge is within 45 days of the account's
  newest transaction. The first detection after the upgrade is quiet about
  everything older.
- **Price changed**: a fixed-price series charged more than 5% (and at
  least 1.00) differently. For monthly and shorter series the next charge
  at the new price confirms it; until then it reads "not confirmed yet".
  Changes under 2% are ignored, and between 2% and 5% the amount is updated
  without a word. Variable bills are not watched for price.
- **Missed**: the expected date plus a few days of grace (3 for weekly and
  fortnightly, 5 for monthly, 10 for two-monthly and quarterly, 21 for
  longer) has passed on the account's own clock without the charge.
- **Ended**: two whole cycles without a charge.
- **Charged after you ended it**: a charge at the old price on a series you
  marked ended.
- **Renews soon**: a week before a half-yearly or yearly charge.
- **Trial became paid**: a first charge of at most 1.00 from a known
  subscription followed by a full one within about a month. Best effort:
  most statements never print the trial.

Four notification kinds can be switched on per channel (Settings,
Notifications): a new subscription, a price change, a missed or stopped
charge, a renewal within a week. As with every notification, the sentence
names nothing: no merchant, no amount, no date, no account. The detail is in
the app ([notifications](security/notifications.md)).

The monthly and yearly totals are in the household's currency. A series in
another currency with no exchange rate on file is named under "Not
included" instead of being silently left out of the sum.

## How good is it

Two sets of test data are part of the repository, written before the
merchant list and the detector existed and by other hands, so they measure
instead of mirroring: 1,200 labelled bank descriptions (800 lines of
well-known merchants in the shapes banks print, 250 of invented local
shops, 150 that are not purchases), and a two-year ledger with 40 planted
series and 25 decoys (weekly groceries, a coffee habit, fuel, a monthly
transfer to savings, a card payment, rent by Zelle). The figures below are
tests: a change that drops one under its target fails the build
(`backend/tests/unit/test_categorisation_quality.py`,
`backend/tests/integration/test_subscription_quality.py`).

| Measure | Before rc.10 | Now | Target |
| --- | --- | --- | --- |
| Known merchants given the right category as a fact | 0% | 93.1% | at least 85% |
| Facts that are right | none stated | 100% | at least 97% |
| Suggestions that are right | none made | 100% | at least 80% |
| Invented merchants given a wrong fact | 0% | 0% | at most 1% |
| All lines with a fact | 0% | 70.4% | at least 60% |
| Lines of one merchant that share one key | 44.0% | 97.6% | at least 95% |
| Non-purchase lines recognised as such | 0% | 100% | at least 97% |
| A person's name in a merchant key | 10 lines | none | none |
| Same merchant right after one correction | 0% | 96.0% | at least 95% |
| Lines of another merchant answered by that correction | 0 | 0 | none |
| Planted series found (found or suggested) | 57.5% | 100% | at least 90% |
| Planted series found without asking | 57.5% | 95.0% | at least 80% |
| Decoys shown as found / as suggested | 6 / 0 | 0 / 1 | 0 / at most 2 |
| Bill, subscription or income told apart | not told | 100% | at least 90% |
| Planted price changes flagged / false ones | 0 of 5 / 0 | 5 of 5 / 0 | all / 0 |
| Stopped series ended / ended wrongly | 0 of 4 / 0 | 4 of 4 / 0 | all / 0 |

What these figures are **not**:

- They are not a promise about your statements. The test lines are
  invented in the shapes banks print; your bank may print a shape nobody
  wrote down, and a local shop is by definition not in a list of national
  merchants. Expect less than 93% on a real first import, with the rest
  left uncategorised or suggested rather than wrong.
- "Facts that are right: 100%" is measured on 1,200 lines. It says no wrong
  fact was found there, not that none is possible. That is why every
  category shows its source, why a store that sells everything is only ever
  a suggestion, and why one correction outranks the list for good.
- The subscription rows are measured with the household's person filing the
  transactions the engine left open, as a person would. With nobody filing
  anything, which is what you see right after a first import, the same run
  tells the kind apart for **92.5%** (37 of 40) instead of 100%: rent,
  water, sewer and after-school tuition at merchants no list knows are bills
  by the category suggested for their rows (a bill's category suggested is
  enough to call a series a bill; it changes nothing else about it). A car
  loan whose entry says only `RETAIL PMT`, a local climbing gym and a swim
  school are found, at the right rhythm and amount, but as "other" on Plan,
  Upcoming instead of on the Bills or Subscriptions tab, until one of their
  transactions is given a category or the series is told "This is a bill".
  The other rows are equal in both runs (all 40 found, 38 without asking, no
  decoy found, one suggested). Both runs are gates: the unfiled one holds
  every bar and holds the kind at 92.5%, so it cannot get worse unnoticed.
- Nothing is measured outside the United States.

## What is not there in 1.0

A model for categories of any kind; merchants outside the United States;
merchant category codes; payees created automatically; merchant logos; an
editor for the merchant list in the app; a per-merchant "do not learn"
switch; guesses for the lines of a split; cancelling a subscription for
you; a report by merchant (planned with the reports of the next stage).
