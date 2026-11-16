# Categorisation and subscriptions (v1.0.0 stage 2)

Status: direction approved by the owner on 2026-11-12; this note settles the
details. Target release: `v1.0.0-rc.10`.

This is the second of four stages on the way to `v1.0.0`: import reliability
(done, rc.9), categorisation and subscriptions (this document), the UI
redesign, and the release review. Each stage has its own design note and plan.

## Why

The owner's words: "better detection of subscriptions and categorization of
transactions aka food, gas, entertainment, etc."

What the code does today (audit of rc.8, checked again on this branch; the
categorisation, rules, recurring and notification packages have not changed
since, only stage 1's tombstones and document filter were added to the
ledger):

- A new household's first import lands with no category and no payee on any
  row. A category comes only from the file, from a rule a person wrote, or
  from the default category of a payee a person created by hand.
- There are five different text normalisers and none knows what a merchant
  is. `SQ *`, `TST*`, `POS DEBIT`, store numbers and city tails defeat all of
  them.
- "Always categorise like this" in the grid builds its words in the browser.
  Three of its five own test examples produce a rule that cannot match the
  row it came from, the preview count can never exceed 1, and a dead rule is
  saved without a sign. Only owners and admins see it.
- A transaction does not record why it has its category. Re-applying rules
  overwrites categories a person chose.
- Recurring detection writes whatever it finds as an active schedule: weekly
  groceries are a "subscription", a deleted one comes back the next night, a
  stopped one stays active for ever, a price change is silent, an annual
  charge needs two years. There is no subscription concept, no alert, and no
  page.

## Goals

1. A first import is mostly categorised with no set-up, from data that ships
   with PerFin and never leaves the host.
2. Never wrong silently: every category shows how it was decided and how sure
   PerFin is; an unsure guess is a suggestion, not a fact; what a person chose
   is never overwritten by a guess.
3. One correction teaches: the next charge from that merchant is right, and
   "always" is one step from the row, for members too.
4. Subscriptions have their own view: more of them found, habits and
   transfers kept out, a person's "this is not one" final, and alerts when
   something changes.
5. Stage 3 only rehouses: the backend, the API and the working components are
   complete in the current layout.

## Non-goals for 1.0

- A model for categories, hosted or local. The optional local model
  (ADR-0010) stays what it is: hints about statement wording. The audit found
  no local categoriser worth keeping, so "learned" is a counted memory.
- Anything shared between households or instances, and any lookup service.
- Merchants and descriptor shapes outside the United States.
- Merchant category codes and a bank's own category column as a prior (most
  statements carry neither; a CSV category column keeps working as today).
- Payees created automatically, merchant logos, a merchant editor, a
  per-merchant "do not learn" switch, guesses for the lines of a split.
- Cancelling a subscription for the person, shared-cost splitting, an `.ics`
  feed, price changes across currencies.
- The new navigation and the Inbox (stage 3). A report by merchant (stage 3,
  with Reports).
- The review queue's `GET /review/{id}/rule-draft`: its button is gone since
  stage 1 and the route is left alone.

## Words

- **Descriptor**: the text a bank printed, kept unchanged in
  `transaction.raw_payee`.
- **Merchant key**: the normalised identity of who was paid (`netflix`,
  `trader joes`, `corner grocer`): lower-case tokens of `a-z0-9` joined by
  single spaces, at most 64 characters.
- **Decided** row: a person, the file, a rule or a payee default set its
  category, or it was categorised before this release. **Undecided**: no
  category and nobody decided. **Guess**: a category from the learned memory,
  the dictionary or a keyword.
- **Fact** and **suggestion**: a guess with confidence of at least 0.80 is
  written to `category_id`; below that it is written to
  `suggested_category_id` only, and counts in no report or budget.

## 1. Default categories

Decision: keep the shipped tree and its stable keys, and add what a merchant
dictionary needs. Two levels, 12 groups, 68 leaves (55 today plus 13).
Reason: `category.system_key` (`food.groceries`) is already the stable
handle that survives renaming, households already hold these rows, and no
other product's list is licensed for reuse.

Added leaves: `food.fast_food`, `food.alcohol`, `food.delivery`;
`shopping.general`, `shopping.home`; `leisure.games`, `leisure.news`,
`leisure.software`, `leisure.memberships`; `utilities.other`;
`housing.improvement`; `financial.services`; `transfers.cash`.

"Subscription" is not a category. A Netflix charge is `leisure.subscriptions`
(named "Streaming" for new households; the key does not change) and belongs
to a subscription series. Reason: one home per transaction; Monarch, Copilot
and Rocket Money all keep "recurring" apart from the category.

Money that is not spending is recognised from the descriptor's kind (section
3), not from a merchant, and goes to fixed categories whose kind keeps them
out of spending totals:

| Descriptor kind | Category | Category kind |
|---|---|---|
| card payment | `transfers.credit_card_payment` | transfer |
| transfer between own accounts | `transfers.between_accounts` | transfer |
| loan payment | `transfers.loan_payment` | transfer |
| cash, ATM | `transfers.cash` | transfer |
| payroll (inflow) | `income.salary` | income |
| interest | `income.interest` (in), `financial.interest_paid` (out) | income, expense |
| bank fee | `financial.bank_fees` | expense |
| refund naming no merchant | `income.refunds` | income |
| refund from a known merchant | that merchant's category (nets against it) | expense |
| cheque, deposit, person-to-person | none, never guessed | |

Existing households: nothing is renamed, moved or re-created. The first
refresh after the upgrade (section 6) adds only the 13 new leaves, each under
the group that carries the parent key. A group the household removed gets
nothing. A category of theirs with the same name under that parent takes the
key instead of getting a twin. A household that deleted a default keeps it
deleted. A household with its own tree gets no dictionary facts for keys it
does not have (the lookup falls back to the parent key, then to nothing) and
is served by learning. Merging a keyed category into an unkeyed one moves the
key.

## 2. The merchant dictionary

- **Where**: `backend/app/domain/categorisation/data/`: `meta.yaml` (the
  version), `merchants/<sector>.yaml`, `keywords.yaml` and `README.md`
  (sources and attributions). Loaded and indexed once per process. No build
  step and no generated artefact, so there is nothing to drift. Processor
  prefixes and descriptor-kind patterns are part of clean-up (section 3) and
  live in its code.
- **Entry**: `key`, `name` (display), `category` (a default `system_key`),
  `match` (one or more token sequences in merchant-key form; `prefix` by
  default, `anywhere` when marked), and optionally `series`
  (`subscription` or `bill`, with usual cadences), `mixed: true` (a store
  that sells everything: Amazon, Walmart, Target, Costco) and `inflow`
  (a payroll processor).
- **Matching**: literal tokens only, no regular expressions (no ReDoS, and a
  reviewer can read every line). The longest match wins, so `amazon prime`
  beats `amazon`; two equally long matches of different merchants are no
  match.
- **Size for 1.0**: at least 800 merchants, aim 1,000, cap 1,500; about 45
  processor and marketplace prefixes; about 120 keywords; about 60 descriptor
  kind patterns in clean-up. This is what a small team can curate and review
  by hand, and
  it covers the national chains, utilities, insurers, telecoms and the 150 or
  so subscription services that make up most of a household's rows.
- **Licence and sources**: the files are our own work under AGPL-3.0-or-later.
  Allowed inputs: what the authors know about brand names and the descriptor
  shapes banks print; `osmlab/name-suggestion-index` (BSD-3-Clause, notice
  kept in the README); Wikidata (CC0); `greggles/mcc-codes` (Unlicense) and
  the IRS list (public domain) as reference. Not allowed: Plaid, MX, Yodlee,
  Ntropy or any enrichment product's data or descriptions; the Visa and
  Mastercard manuals and ISO 18245; OpenStreetMap extracts (share-alike);
  data sets of unclear origin; another product's database, scraped or
  exported; any line of a real person's statement or of the scratch corpus.
  Brand names are used to name the merchant, nothing more.
- **Review**: a unit test is the gate: schema, unique keys, token form, every
  category a default key, no match that equals a keyword or a processor
  prefix, and a pinned content digest that must change together with the
  version. Every change is a normal pull request.
- **Versioning and updates**: `meta.yaml` holds an integer. Each household
  records the dictionary, clean-up and taxonomy versions it was last
  evaluated with. An hourly dispatcher queues a refresh for a household
  whose versions are behind. The refresh recomputes merchant keys and
  re-evaluates **only** rows nobody decided: undecided rows, suggestions, and
  facts whose source is `dictionary` or `keyword`. It never touches a row
  whose source is `person`, `file`, `rule`, `payee`, `learned` or `legacy`,
  nor a split row. It is audited with counts.

## 3. Payee clean-up

Pure functions in `categorisation/cleanup.py`, no database, no I/O.
`clean(raw) -> Cleaned(core, kind, via, signals)`; the raw descriptor is never
changed or dropped.

Steps, in order: (0) NFKC, upper case, whitespace, non-printing characters;
(1) bank transaction-type prefixes, recorded as the **kind** (purchase,
recurring purchase, ACH debit or credit, payroll, transfer, card payment,
loan payment, cash, cheque, fee, interest, refund, person-to-person, deposit,
unknown); (2) labelled ACH layouts (`ORIG CO NAME:`, `DES:`, `INDN:`): the
company is kept, the individual's name is dropped; (3) bank trailers: card
suffixes, reference numbers, dates and times inside the string; (4)
processor and marketplace prefixes (`SQ *`, `TST*`, `PAYPAL *`, `DD *`,
`APPLE.COM/BILL`, ...), recorded as `via`; (5) phone numbers, URLs, state,
city and ZIP tails; (6) store, terminal and order numbers; (7) company
suffixes, punctuation and possessives. What is left is the **core**.

`engine.identify(raw)` looks the core up in the dictionary. The **merchant
key** is the dictionary's key on a hit, else the first four tokens of the
core. The **display name** is the dictionary's name on a hit, else the key
in title case. For a person-to-person payment the key is the service
(`zelle`), never the other person. The key is stored
(`transaction.merchant_key`); the name is computed when a row is read.

The four older normalisers keep their jobs (the import hash must not move).
The browser's `descriptionKey` is deleted. Payee recognition runs on the
core, so `PAYPAL *NETFLIX` finds the payee Netflix instead of none.

## 4. How a category is decided

Order, first that answers wins:

| # | Source | Stored as | Confidence |
|---|---|---|---|
| 1 | A person's choice (also: clearing it, accepting a suggestion, splitting) | `person` | 1.00 |
| 2 | A category column in the file | `file` | 0.95 |
| 3 | A household rule, in rule order | `rule` (+ rule id) | 1.00 |
| 4 | The default category of the row's payee | `payee` | 0.95 |
| 5 | Learned from this household's corrections | `learned` | 0.80 / 0.90 / 0.95 for 1 / 2 / 3+ decisions, times the winner's share |
| 6 | Dictionary (merchant or descriptor kind) | `dictionary` | 0.90; 0.85 for an `anywhere` match; 0.05 less behind a processor; 0.60 for a `mixed` store |
| 7 | Keyword | `keyword` | 0.60; 0.80 for the few strong ones (`TST*`) |
| 8 | Nothing | NULL | |

Rows categorised before this release are `legacy`: never overwritten by a
guess, and a re-applied rule may still change them as it does today.

Stored per transaction: `merchant_key`, `category_source`,
`category_confidence`, `category_rule_id`, `suggested_category_id`. The grid
shows the source in words ("You chose", "Rule: Coffee", "Learned from you, 3
times", "Merchant list", "Keyword"), and a suggestion as a suggestion with
one-step accept.

**Learned** is a table, `merchant_memory`: per household, merchant key and
direction (money out, money in), one row per category with `decisions` and
`last_decided_at`. A person's category choice on a row with a merchant key
adds one; changing that choice later moves it. The answer for a key is the
category with the most decisions (most recent on a tie), with
`share = its decisions / all decisions for the key`. Share below 0.60 means
the household files this merchant in several places: no learned answer, and
the dictionary's answer becomes a suggestion. Decisions older than three
years are a suggestion. It is deterministic, it can be listed, each entry
can be forgotten, and it survives deleting and re-importing a statement
(which is why it is a table and not a query over the ledger). On the first
refresh it is seeded from the household's `legacy` categories, so years of
hand work count from day one. Bounded at 5,000 rows per household. An entry
with three or more decisions and share 1.00 that no rule covers is offered
as a rule.

**The `updated_at` invariant.** Stage 1 defines "untouched since import" as
`updated_at == created_at`, and reprocessing removes only untouched rows. A
categorisation pass must therefore never look like a person. Decision: every
write the engine makes (merchant key, guess, suggestion, the link to a
subscription) goes through one function that issues
`UPDATE ... SET ..., updated_at = updated_at`, and never through the ORM
(whose `onupdate` would move the timestamp). During an import the importer
still equalises the timestamps last. A person's choice goes through the
transactions service and moves `updated_at`, as today. A rule re-applied on
request keeps moving it (stage 1, decision 6). Tests pin all three.

## 5. Rules in context

- **"Always categorise *merchant* as..."** from a row, for members and above.
  It creates an ordinary rule with a new condition, `merchant` (the key), and
  one `set_category` action. Reason: one rules list, one order, one export.
- **Preview before saving**, computed on the server over the whole ledger:
  how many existing rows have no category, how many carry a guess that would
  change, and how many a person decided (these never change). The counts are
  real, not capped.
- **Apply to existing is a separate, unticked choice.** It runs the existing
  rule job for that one rule over accounts the caller may edit.
- **Order and conflicts**: a new merchant rule goes last. If an enabled rule
  for the same key exists, it is updated instead of doubled. If an earlier
  rule already categorises the source row, saving is refused and that rule is
  named.
- **Undo**: `POST /rules/{id}/revert` deletes the rule and returns every row
  whose category came from that rule to undecided, then lets the engine
  guess again. Provenance makes this exact at any later time. Undoing a
  changed rule puts its previous category back.
- **Re-apply respects people**: the rule job no longer changes the category
  of a row whose source is `person`.
- The old dialog and its key builder are removed.

## 6. Subscriptions

**Detection** (pure functions, replacing the current detector):

1. Input: rows of the last 1,100 days that are not transfer-linked, whose
   category kind is not transfer or ignore, and whose descriptor kind is not
   transfer, card payment, cash, cheque, fee, interest or deposit.
2. Group by account, merchant key and direction. Payee ids are not used, so
   history no longer splits when a payee is created.
3. Cluster amounts: a row joins while within 7.5% of the cluster's running
   mean (at least 0.50). Two plans at one merchant are two series.
4. Cadence by median gap, with at least 80% of gaps inside the window:

   | Cadence | Gap (days) | Charges needed | With a known merchant |
   |---|---|---|---|
   | weekly | 6-8 | 4 | 4 |
   | fortnightly | 12-16 | 3 | 3 |
   | monthly | 26-35 | 3 | 2 |
   | every two months | 55-66 | 3 | 2 |
   | quarterly | 83-99 | 3 | 2 |
   | half-yearly | 170-195 | 2 | 2 |
   | annual | 350-380 | 2 | 1 (suggested only) |

5. Amount: fixed (spread within 1%, at least 0.50), variable (within 7.5%),
   or wide. A wide series qualifies only as a bill.
6. **Kind**: `income` (money in); `bill` (rent, mortgage, loans, insurance,
   utilities, phone, internet, childcare, taxes, a dictionary bill, or a
   variable amount); `subscription` (fixed amount and a dictionary
   subscription or a discretionary category); `other`.
7. **Not a series**: groceries, restaurants, fast food, coffee, alcohol,
   delivery, fuel, parking, taxis, transit, general shopping and cash are
   habits. They are dropped unless the amount is fixed, every gap is regular
   and there are six charges, and then only suggested. More than one charge
   per cycle in a cluster is dropped. A person-to-person series is a
   suggested bill at most.
8. Confidence: `0.4 regularity + 0.3 stability + 0.2 count + 0.1 known`.

**States**, stored on `scheduled_transaction.status` with who set it:
`suggested` (shown under "Might be", counted nowhere), `active` (found with
enough evidence, counted), `confirmed` (a person said yes), `paused`,
`ended`, `dismissed` ("not a subscription"). The existing `active` flag stays
and is always `status in (active, confirmed)`, so the calendar, the budget
forecast and the AI view keep working unchanged.

**A person's decision sticks.** The detector matches what it finds to
existing series by account, merchant key, direction and overlapping amounts,
and never creates a second one. It never changes a status or a kind a person
set, with one factual exception: a confirmed series whose charges stop is
marked ended, with an alert. Deleting a detected series dismisses it. A
dismissed one never returns.

**The clock is the data, not the wall.** PerFin has no bank feed; rows arrive
when a statement is imported. "Missed" and "ended" are judged against the
latest row of the account, never against today, so a subscription is not
"stopped" because nobody uploaded November yet.

**Changes found**, each stored once as a `subscription_event`:

- `new`: a series first seen in the 45 days before the account's latest row.
  The first detection after the upgrade is quiet about older ones.
- `price_changed`: a fixed series charged more than `max(1.00, 5%)`
  differently. Confirmed by the second charge at the new price, or at once
  for half-yearly and annual series. Drift under 2% is ignored; between 2%
  and 5% the amount is updated without an alert.
- `missed`: the expected date plus grace (3 days weekly and fortnightly, 5
  monthly, 10 two-monthly and quarterly, 21 longer) passed with rows after it
  and no charge. `ended` after two missed cycles.
- `charged_after_end`: a charge on a series a person marked ended.
- `renewal_due`: seven days before a half-yearly or annual charge.
- `trial_converted`: a first charge of at most 1.00 from a dictionary
  subscription followed within 3 to 35 days by a full one. Best effort:
  most statements never print the zero-value hold.

**Alerts** use the existing notification channels, with four new kinds
(`subscription_new`, `subscription_price_changed`, `subscription_missed`,
`subscription_renewal`) and fixed sentences that carry no name, amount, date
or account, as the notification rules require. The detail is in the app.
There is no new outbound channel.

**The view** (`/subscriptions` in the current shell; stage 3 moves it under
Plan): monthly and yearly cost in the household's currency and the count;
"Needs a look" (suggested series and open changes, with Confirm / Not a
subscription); upcoming in the next 30 days; active, sortable by yearly cost;
bills on their own tab; ended and dismissed folded away. A series opens to
its charges, its price history and its next expected charge, and links to
its transactions. It reads on a phone as stacked cards.

## 7. Data model

One migration, `0014` (`down_revision = "0013"`).

- `transaction`: `merchant_key`, `category_source` (`person`, `file`, `rule`,
  `payee`, `learned`, `dictionary`, `keyword`, `legacy`),
  `category_confidence`, `category_rule_id`, `suggested_category_id`,
  `scheduled_transaction_id`. A suggestion exists only while there is no
  category (check). Indexes on `(household_id, merchant_key)` and on the
  series link. Backfill: `legacy` for every row with a category or a split.
- `scheduled_transaction`: `kind`, `kind_source`, `status`, `status_source`,
  `merchant_key`, `first_seen_on`, `occurrences`, `confidence`,
  `last_amount`, `previous_amount`, `price_changed_on`, `ended_on`; a check
  ties `active` to `status`. Backfill from `active` and `origin`.
- `rule` and `scheduled_transaction` gain `UNIQUE (household_id, id)` so the
  new references are household-consistent (ADR-0008).
- `merchant_memory` and `subscription_event`: new, household scoped, forced
  row-level security.

## 8. API, MCP, portability, authorisation, audit

- **API**: transactions gain the provenance fields, four filters and a bulk
  "accept suggestions". New: `/categorisation/summary`, `/learned`,
  `/learned/{id}` (forget), `/refresh`; `/rules/merchant/preview`,
  `/rules/merchant`, `/rules/{id}/revert`; `/subscriptions`,
  `/subscriptions/events`, `/events/seen`, `/{id}`, `/{id}/decision`. Twelve
  routes; the plan gives the shapes.
- **MCP**: the tool count stays 19 and the AI read API stays at 19 routes.
  Deliberate change: `get_recurring` rows gain `kind` and `status`, so an
  assistant can tell a subscription from a bill and an ended one from an
  active one. `merchant_key`, the memory and the events are not exposed.
- **Portability**: the archive format becomes 3. A person's decisions travel:
  the new columns of transactions (except the two instance-local links) and
  of scheduled transactions, and `merchant_memory`. `subscription_event` is
  not exported (detection finds the changes again). Archives of format 1 and
  2 import with the new columns empty.
- **Authorisation**: one policy line per route. Reading needs viewer;
  correcting, forgetting, deciding and making a merchant rule need member;
  refresh and reverting a general rule need admin. One new parameter kind
  (`merchant_memory`). Every list and count is limited to accounts the caller
  may see.
- **Audit**: `transaction.categorise` (counts by source), `categorise.refresh`,
  `memory.forget`, `rule.revert`, `subscription.decide`, and the existing
  rule and schedule actions. Ids, counts and category ids only: never a
  descriptor, a merchant key or a name.
- **Privacy**: nothing leaves the host. The dictionary is static data in the
  image; no request is made to look anything up; notification sentences are
  generic. `docs/security/what-leaves-the-host.md` says so.

## 9. Measurement

Two committed, synthetic sets, written before the dictionary and by other
hands, so they measure instead of mirroring:

- **1,200 labelled descriptors**: 800 naming well-known merchants in the
  shapes six kinds of bank print, 250 invented local merchants, 150
  non-purchases. Written by us; store numbers, references and people are
  invented; no line comes from a statement.
- **A 24-month ledger** with 40 planted series (monthly, annual, weekly,
  two plans at one merchant, price changes, stopped, a trial, variable
  utilities, rent, a loan) and 25 decoys (weekly groceries, a coffee habit,
  fuel, a monthly transfer to savings, a card payment, rent by Zelle).

| Measure | Today | Target |
|---|---|---|
| Known merchants given the right category as a fact | 0% | at least 85% |
| Facts that are right (precision), whole set | n/a | at least 97% |
| Suggestions that are right | n/a | at least 80% |
| Invented merchants given a wrong fact | 0% | at most 1% |
| All lines with a fact | 0% | at least 60% |
| Lines of one merchant sharing one key | n/a | at least 95% |
| Non-purchase kind recognised | n/a | at least 97% |
| A person's name in a merchant key | n/a | none |
| Merchant repeated after one correction: right | 0% | at least 95% |
| Planted series found (active or suggested) | measured at Start | at least 90% |
| Planted series found as active | measured at Start | at least 80% |
| Decoys shown as active / as suggested | measured at Start | 0 / at most 2 |
| Series kind right | n/a | at least 90% |
| Planted price changes flagged / false ones | 0 / n/a | all / 0 |
| Stopped series ended / ended wrongly | 0 / n/a | all / 0 |

The "today" column for subscriptions is measured by running the current
detector on the ledger before any lane starts. The targets are tests in the
final phase.

The scratch corpus of public sample documents gives a third, aggregate-only
reading: its payee strings are run through clean-up and the engine outside
the repository, and only percentages are reported (share with a merchant
key, with a dictionary hit, with a fact, with a suggestion). Indicative
targets: a key for 80% of purchase-like rows, a fact or suggestion for 45%.
Two hundred random facts are read by eye; more than 3% wrong blocks the
release candidate. The corpus is look-alike samples with many invented
merchants, so the figures are stated with that caveat. Its strings never
enter the repository or a pull request.

## Risks

- A wrong fact at scale. Mitigation: the 0.80 line, `mixed` stores as
  suggestions, the precision gate, provenance on every row, exact undo.
- The engine marking rows as touched and breaking reprocessing. Mitigation:
  the single write function, and tests on the invariant in three lanes.
- A dictionary of 1,000 hand-written entries is real work and will have
  errors. Mitigation: the validator, the independent labelled set, the
  version and the refresh that corrects only what nobody decided.
- Subscription detection on statement data that arrives in monthly lumps.
  Mitigation: the data clock; suggested before active for thin evidence.
- The archive format bump. Mitigation: format 1 and 2 archives are tested.
  Known and not fixed here: the archive reader wants exactly the columns of
  the running version, so a format 2 archive written before migration `0009`
  added `account.subtype` is, by reading the code, already refused. The plan
  checks this and the upgrade note says what to do.
- Members can now create merchant rules. It is the power they already have
  through a payee's default category; "apply to existing" is limited to
  accounts they may edit.
