# Finding your way around

PerFin has six sections and Settings. Each piece of data has one home, and
every address PerFin used before 1.0.0-rc.11 still works: it forwards to
that home and keeps the rest of the address (the part after `?` and after
`#`), so bookmarks and links in old notifications keep working.

This page says what lives where. It describes the screens in words; the
repository keeps no screenshots.

## The sections

The rail on the left holds, in order:

| Section | Address | What it holds |
| --- | --- | --- |
| Home | `/` | Where your money stands, what needs you, this month's spending, what is coming up and the latest transactions |
| Inbox | `/inbox` | Everything that waits on a person, the document library and the history of answered items |
| Transactions | `/transactions` | Every transaction of every account, in one list |
| Accounts | `/accounts` | Your accounts by group, each with its own page, and the investments of all accounts together |
| Plan | `/plan/subscriptions` | Subscriptions, budgets, goals and what is coming up |
| Reports | `/reports` | Spending, income, cash flow, trends, net worth and tax |

At the foot of the rail: **Settings**, the account menu (your name, the
household switcher, Profile and Sign out) and the theme toggle.

A section with more than one page has a row of tabs under its heading. A
page about one thing (an account, a document, a budget, a rule, a tax year)
shows breadcrumbs instead, such as "Inbox / Documents / file name".

### Home

Home is one fixed page. Nothing on it can be hidden or moved.

- **Net worth**: the figure, its change since the start of the month, a
  12-month line and a link to Accounts.
- **Needs you**: the first three Inbox cards, one line each, and a line for
  transactions that have no category or a suggestion. Each card's line
  opens the Inbox at that card. "Open Inbox" goes to the rest.
- **Spending this month**: the month so far, compared with the same day of
  last month, and the largest categories.
- **Coming up**: the next charges in 30 days, from your subscriptions and
  recurring items. An amount that varies is marked as an estimate.
- **Latest transactions**: five rows, each opening the transaction, and
  "All transactions".

A household with no accounts yet sees "Start here" instead.

Each panel loads on its own. A panel that cannot load says so in one line,
with Retry, and the others still show.

### Inbox

The Inbox gathers everything that waits on a person. Its count is on the
rail.

- **Needs you** (`/inbox`): an Upload button at the top opens the file
    picker. Then one card per thing to answer, most urgent first:

    1. documents whose money is wrong until someone answers (a statement
       that does not add up, rows to check, a possible copy, a sign
       question);
    2. documents that cannot import until someone answers (which account,
       which columns, whose file, teach PerFin the layout);
    3. documents with something to note (a receipt to attach, a note to
       acknowledge);
    4. review items that belong to no document;
    5. subscriptions: one card per series that PerFin suggests or that
       changed (price, missed, ended, charged after you ended it, trial
       became paid, renews soon);
    6. suggested categories: the merchants with the most suggestions, each
       with "Accept N", and a line with the number of transactions that
       have no category;
    7. "Make it a rule": up to three merchants you always file the same
       way, offered as a rule.

    Inside the first three groups the oldest document comes first. A card
    you answered stays where it was until you leave the page, and says what
    happened. Under the cards, **Add documents** has the account choice
    and the drop zone (Home's "Add documents" leads there), then
    **Imported recently** lists the files that finished in the last 14
    days, with their account and period.
- **Documents** (`/inbox/documents`): the library of every file, with its
  kind, status, tax year and a full-text search. A document opens at
  `/inbox/documents/<id>`, with its pages, where its rows went, and its
  column mapping under `/inbox/documents/<id>/mapping`.
- **History** (`/inbox/history`): review items that were answered or
  dismissed.

`/inbox?document=<id>` shows only that document's card, with "Show
everything" to see the rest.

**The count** on the rail is the number of cards that need an answer:
documents, items of no document, subscription series, and one for the
suggested categories. "Make it a rule" cards are offers, not questions,
and are not counted. The count only covers accounts you may see, so two
members of one household can see different numbers. When the count cannot
be loaded, the rail shows nothing rather than zero.

**Not checked against a balance.** A statement that imported cleanly but
printed no balance to check against carries this quiet marker, in
"Imported recently" and on its document page. "Check against the
statement", on the document, lets you type the balances and check it.

### Transactions

One list of every account. Every filter is part of the address, so a
filtered list can be bookmarked or shared with another member: the
account, the category (or "No category"), suggestions only, the merchant,
the subscription, the file it came from, the dates and the search. Choosing
Transactions on the rail clears them.

- **Add transaction** and **Find transfers** are at the top, beside the
  heading. The search field is the first control under it.
- Search, **Suggestions** and **No category** (quick filters with their
  counts), Account and Category share one bar. The dates, payee, tag and
  review state are under **More filters**, which opens by itself when the
  address sets one of them and, while closed, counts the ones set
  ("More filters (2)").
- When a filter is set, a line under the filters adds up money in, money
  out and the net of the rows shown ("of the 48 shown").
- A category always shows how it was decided, in small text under it
  ("Merchant list, 90%", "Learned from you", "You chose this", or the
  rule's name). A suggestion is shown
  as a question ("Groceries?") with Accept. It never looks like a chosen
  category.
- Select rows to change many at once from the bar that appears: category,
  tags, accept suggestions, mark confirmed, delete.

### The transaction drawer

A click on a row, or Enter on a focused row, opens the drawer at the right
and adds `?tx=<id>` to the address. Escape, the close button or Back closes
it and puts the focus back on the row. On a phone the drawer fills the
screen.

The drawer shows:

- the amount and the date;
- **Account**: a link to the account's page;
- **Bank wrote**: the description exactly as the bank printed it, always
  shown beside the merchant name;
- **Came from**: a link to the file it was imported from, or "Entered by
  hand";
- **Category by**: who or what decided the category, and how sure a guess
  is.

Then the **category**. Choosing one is your decision, and it is never
overwritten by a guess or a rule afterwards. The drawer then says what the
transaction had before. If you had chosen that earlier category yourself,
Undo puts it back. If it was a guess or a rule, "Choose *category* again"
sets it back as your own choice, because a guess cannot be restored as a
guess. "New category…" at the end of the list makes a category without
leaving the drawer (members and above).

**"Always use *category* for *merchant*"** sits under the category, and is
unticked. Ticking it asks the server what the rule would meet and shows it
before anything is saved: how many of the merchant's transactions have no
category, how many carry a guess that would change, and how many you or a
rule decided (those never change). A second box, also unticked, offers to
change the existing transactions too. Save makes the rule. When the save
made a new rule, Undo right after removes it and returns what it
categorised to PerFin's guesses. When no rule can
be made from the row (no merchant, a transfer, a split) or an earlier rule
already decides it, the drawer says why and links to that rule.

Under that are the row's tags, and the buttons **Edit details** (account,
date, status, amount, payee, description, memo), **Split**, and a menu with "Mark as transfer",
the note and Delete.

A link of the form `/transactions?always=<id>` (the Inbox's "Make it a
rule" uses it) opens the drawer for that row with the "Always use…" box
already ticked and its preview shown. Nothing is saved until you press
Save.

Viewers see the same drawer without the controls that change anything.

### Accounts

- **All accounts** (`/accounts`): your net worth with its change over the
  last month and a link "Net worth over time" to Reports, then the
  accounts in groups, each with its total: Cash, Cards and credit,
  Investments, Loans, and Property and other. Each row says how current its
  balance is ("Balance on Nov 12", or "Your estimate" for an account you
  keep by hand). Closed accounts fold away under "Closed". "Add account"
  also creates loans and assets. `/accounts?group=loans` scrolls to a group.
- **Investments** (`/accounts/investments`): holdings, performance,
  activity and prices across all investment accounts, with an account
  filter.

**Each account has one page**, `/accounts/<id>`, with tabs that depend on
its type:

| Type | Tabs |
| --- | --- |
| every account | Overview (balance chart and recorded balances), Transactions (the latest 50, and a link to all of them), Documents |
| brokerage, retirement | also Holdings (with the cost-basis method), Activity, Performance |
| loan, mortgage | also Schedule, Payments, Rate changes, Payoff (the extra-payment simulator) |
| manual asset | also Valuations |

A loan has one page: its account's. Deleting the loan on the Schedule tab
keeps the account, and the dialog says so. Deleting or closing the account
is in the page's header.

### Plan

- **Subscriptions** (`/plan/subscriptions`): what you pay regularly, what
  it costs per month and per year, what needs a look and what ended. A
  series opens at `/plan/subscriptions/<id>`. The series under "Needs a
  look" also appear as Inbox cards. A Subscriptions or Bills toggle at the
  top switches between the two (`?kind=bills`).
- **Budgets** (`/plan/budgets`; one budget at `/plan/budgets/<id>`).
- **Goals** (`/plan/goals`).
- **Upcoming** (`/plan/upcoming`): the cash-flow calendar and the recurring
  items (income, bills, and anything else that repeats), as two tabs.

`/plan` opens Subscriptions.

### Reports

- **Spending**, **Income**, **Cash flow** and **Trends** (`/reports`, the
  view in `?tab=`). The period, account and "include pending" choices stay
  in the address and are kept as you switch between these four.
- **Net worth** (`/reports/net-worth`). Each row of the breakdown links to
  its account.
- **Tax** (`/reports/tax`; one year at `/reports/tax/<year>`), with the
  documents still missing for that year.

### Settings

Settings has a list of pages in six groups. On a phone, `/settings` is that
list.

| Group | Pages |
| --- | --- |
| You | Profile, Security, Notifications |
| Household | Name and currency, Members, Account access, Exchange rates |
| Categorisation | Categories (tabs: Categories, Payees, Tags, Tax hints), Rules, What PerFin learned |
| Import | Intake |
| Data and integrations | Export and import, AI access |
| Instance | Users (instance administrators only) |

Rules and categories are made where you work, in Transactions and the
Inbox. Settings keeps them so you can edit, reorder or remove them.

- **Rules** (`/settings/rules`, a new rule at `/settings/rules/new`, one
  rule at `/settings/rules/<id>`), with rule export and import and Undo
  for a merchant rule.
- **What PerFin learned** (`/settings/learned`): how your transactions got
  their category, the categories learned from your corrections, the
  layouts you taught, the saved column mappings and the statement wording
  learned. Owners and admins find "Check again" here, to look again at
  every transaction nobody decided after an update of the merchant list.
- **Tax hints** is the tab of Categories that maps each category to a tax
  schedule line (`/settings/categories?tab=tax`).

## Where the old pages went

Every address on the left forwards to the one on the right. What came
after `?` or `#` in the old address is kept.

| Old address | Now |
| --- | --- |
| `/review` | Inbox, Needs you: `/inbox` (`?document=` kept) |
| `/documents` (upload) | Inbox, Needs you: `/inbox`, where Upload now is |
| `/documents/library` | Inbox, Documents: `/inbox/documents` |
| `/documents/<id>` | `/inbox/documents/<id>` |
| `/documents/<id>/mapping` | `/inbox/documents/<id>/mapping` |
| `/documents/learned` | Settings, What PerFin learned: `/settings/learned` |
| `/investments` | Accounts, Investments: `/accounts/investments` |
| `/loans` | Accounts, the Loans group: `/accounts?group=loans` |
| `/loans/<id>` | the loan's account, Schedule tab: `/accounts/<account>?tab=schedule` |
| `/assets` | Accounts, the Property and other group: `/accounts?group=property` |
| `/assets/<id>` | the asset's account, Valuations tab: `/accounts/<account>?tab=valuations` |
| `/subscriptions` | Plan, Subscriptions: `/plan/subscriptions` |
| `/subscriptions/<id>` | `/plan/subscriptions/<id>` |
| `/budgets` | Plan, Budgets: `/plan/budgets` |
| `/budgets/<id>` | `/plan/budgets/<id>` |
| `/goals` | Plan, Goals: `/plan/goals` |
| `/calendar` | Plan, Upcoming: `/plan/upcoming` |
| `/net-worth` | Reports, Net worth: `/reports/net-worth` |
| `/tax` | Reports, Tax: `/reports/tax` |
| `/tax/<year>` | `/reports/tax/<year>` |
| `/tax/mapping` | Settings, Categories, Tax hints: `/settings/categories?tab=tax` |
| `/rules` | Settings, Rules: `/settings/rules` |
| `/rules/new` | `/settings/rules/new` |
| `/rules/<id>` | `/settings/rules/<id>` |

`/loans/<id>` and `/assets/<id>` first look up the account the loan or
asset belongs to. While they do, a spinner shows; if the loan or asset no
longer exists, the page says so and links to Accounts.

Home, Transactions, Accounts, an account's page, Reports and every
Settings page kept their addresses. The old Dashboard is Home; its
customise dialog is gone (see "What is not there").

## Keyboard

- **Ctrl+K** (Windows, Linux) or **Cmd+K** (Mac) opens the command palette
  from anywhere. Type part of a name and press Enter to go to any section,
  tab, settings page or account. Up and Down move through the list, Escape
  closes it. It finds accounts that a page has already loaded; it does not
  search transactions.
- **Escape** closes the drawer, a dialog or the palette, and returns the
  focus to where it was.
- **In the Transactions list**, Up and Down move between rows and Enter
  opens the drawer.
- **Skip to content** is the first thing Tab reaches on every page.

There are no single-letter shortcuts, so typing never triggers anything by
accident. Every shortcut needs Ctrl or Cmd, or works only inside the list
that has the focus.

## Light and dark

The theme toggle at the foot of the rail (on a phone, in the top bar)
cycles **System**, **Light** and **Dark**. Its name says the current choice
and the next one ("Theme: system. Switch to light."). System follows your
device and changes with it. The choice is kept in this browser only, so
each device can have its own.

The fonts and every colour are part of PerFin itself; nothing is loaded
from another site.

## On a phone

PerFin is built mostly for a desktop, and every feature also works on a
phone.

- **Under 820 px wide** the rail becomes a top bar (PerFin, the account
  menu and the theme toggle) and a bottom bar with the six sections.
  Settings is in the account menu. The drawer fills the screen, and panels
  that sit side by side on a desktop are stacked.
- **Under 720 px**, lists that have a card layout switch to it:
  a transaction is two lines, the merchant and amount, then the date and
  category, with its checkbox and row menu at the sides (the account and
  how the category was decided are in the drawer); subscriptions, accounts and "Imported recently" do the same.
- **From 820 to 1023 px** the rail shows only icons. Their names are still
  read out by a screen reader, and the Inbox count becomes a dot whose
  number is part of the link's name.

## What is not there

By decision, for 1.0:

- **Home cannot be customised.** The dashboard's widget catalogue and its
  customise dialog are gone. A layout you saved stays in the database,
  unread. Budgets, goals, holdings, loans and missing tax documents are one
  click away in Plan, Accounts and Reports.
- **The palette does not search transactions.** Use the search field on
  Transactions.
- **The category is no longer a select in the list itself.** It is changed
  in the drawer, or for many rows at once from the selection bar.
- **The drawer does not say which page and row of a file a transaction came
  from**, only which file.
