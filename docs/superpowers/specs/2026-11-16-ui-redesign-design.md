# UI redesign (v1.0.0 stage 3)

Status: the owner approved the direction on 2026-11-12. On 2026-11-13 he
approved the clickable mock-up ("it looks great"). This note settles the
details. Target release: `v1.0.0-rc.11`.

This is the third of four stages on the way to `v1.0.0`: import reliability
(rc.9), categorisation and subscriptions (rc.10), the UI redesign (this
document) and the release review. Stage 3 builds on stage 2's branch
(`claude/categories-subscriptions`). That branch put the categorisation and
subscription screens into the current layout, so this stage only gives them
a new home.

## Why

In the owner's words, there are "too many locations where data can be
buried". Here is what the code does today. The rc.8 frontend audit was
checked again against this branch; section "What changed since the audit"
in the plan lists what no longer holds.

- **The sidebar is a flat list of 17 entries.** It has no groups, no icons
  and no counts.
- **It reaches 47 URLs.** Three of the pages behind them are reachable
  only from a text link: the library, "what PerFin learned" and the tax
  mapping.
- **One object graph is spread over five sections.** Accounts, Investments,
  Loans, Assets and Net worth hardly link to each other. A loan has two
  detail pages.
- **Forward-looking money lives in four places**: Budgets, Goals, Calendar
  and Subscriptions. Reports is an island.
- **Teaching PerFin happens on 15 surfaces under 6 entries.**
- **The import pipeline is spread over 5 pages**: upload, review, library,
  document detail and mapping.
- **State is not in the URL** except on Reports. Transactions reads four
  filters once and never writes them back.
- **The design system has not changed since rc.4.** It has 24 colours and
  one accent blue, there is no chart palette outside Reports, and three
  page vocabularies exist side by side (`ledger-*`, `planning-*`,
  `settings-*`).
- **There is no code splitting.** Before stage 2 the app was one 1,322 kB
  bundle (377 kB gzip).
- **Some write buttons ignore roles.** Budgets, Goals and Categories show
  write buttons to viewers (`useCanWrite` has no caller there).

## Goals

1. **Every piece of data has exactly one home**, at most two clicks from
   the rail. Every current URL either stays or redirects to that home, so
   bookmarks and links in old notifications keep working.
2. **Six sections plus Settings**: Home, Inbox, Transactions, Accounts,
   Plan, Reports.
3. **One place gathers everything that needs a person**: the Inbox, with
   its count on the rail.
4. **A modern consumer-finance look in light and dark**, built toward the
   approved mock-up and meeting WCAG 2.2 AA.
5. **The trust principle is visible on every screen.** Nothing is wrong
   silently, a guess shows how it was decided, and a person's choice is
   never overwritten.
6. **Mostly desktop, readable on a phone.** Every feature works on both,
   and none is phone-only.
7. **A smaller first load.** Charts and the import tools load only when
   their pages open.

## Non-goals for 1.0

- New features beyond what the mock-up shows and stage 2 delivered: a
  budget editor redesign, goal projections, new report types, a report by
  merchant, or a "since your last visit" digest.
- A full-text search across the ledger. The command palette navigates; it
  does not search rows.
- A customisable Home. The dashboard's widget catalogue, its customise
  dialog and the saved `dashboard_layout` preference go out of use. The
  preference stays in the database and the API, unread.
- Showing the page and row a transaction came from ("page 2, row 14" in
  the mock-up). `TransactionOut` carries only `source_document_id`.
- Row counts per document in lists ("38 trades" in the mock-up). Counts
  are on the document page (`GET /documents/{id}/imported`), not in the
  list.
- A "possible duplicate charge" alert for subscriptions. The mock-up shows
  one; stage 2 has no such event kind.
- Assigning Inbox items to a household member, per-person views, a
  pinnable rail, swipe gestures and offline editing.
- Institutions as a concept the person can see or edit (there is no
  endpoint).
- A merchant editor and merchant logos (stage 2 non-goals, unchanged).

## Words

- **Section**: one of the seven rail destinations (Home, Inbox,
  Transactions, Accounts, Plan, Reports, Settings).
- **Sub-page**: a page inside a section, reached from the section's tab bar
  (Plan, Budgets) or its side list (Settings, Rules). A sub-page has its
  own path.
- **Tab**: a view inside one page, kept in `?tab=` (an account's
  Payments, the categories page's Payees).
- **Detail page**: one object (an account, a budget, a document, a rule,
  a tax year). A detail page has breadcrumbs and no tab bar of its
  section.
- **Drawer**: a panel over the right edge of a list that shows one row
  without leaving the list. On a phone it is a full-screen sheet.
- **Inbox card**: one thing that waits on a person. Documents get one card
  each.
- **Redirect**: an old URL answering with the new one (`replace`), keeping
  its search and hash.

## 1. Information architecture

### 1.1 The rail

In order: **Home, Inbox, Transactions, Accounts, Plan, Reports**. Then, at
the foot of the rail: **Settings**, the account menu and the theme toggle.

The account menu holds the person's name, the household switcher, Profile
and Sign out (the existing `AccountMenu`). The Inbox entry carries a badge
with the number of Inbox cards, hidden at zero (section 4). The current
section is `aria-current="page"`.

### 1.2 Route map

Every URL the app has today has exactly one row. Old URLs redirect, keeping
their search and hash. Two of them first look up the account (marked
"data").

| Today | Section | New URL | How |
|---|---|---|---|
| `/` | Home | `/` | same |
| `/inbox` (new) | Inbox | `/inbox` | new: Needs you |
| `/review` | Inbox | `/inbox` | redirect (`?document=` kept) |
| `/documents` | Inbox | `/inbox` | redirect (the uploader is on the Inbox) |
| `/documents/library` | Inbox | `/inbox/documents` | redirect |
| `/documents/$documentId` | Inbox | `/inbox/documents/$documentId` | redirect |
| `/documents/$documentId/mapping` | Inbox | `/inbox/documents/$documentId/mapping` | redirect |
| `/inbox/history` (new) | Inbox | `/inbox/history` | new: answered and dismissed items |
| `/transactions` | Transactions | `/transactions` | same; filters and `tx` in the URL |
| `/accounts` | Accounts | `/accounts` | same; `?group=` scrolls to a group |
| `/accounts/$accountId` | Accounts | `/accounts/$accountId` | same; account hub, `?tab=` |
| `/investments` | Accounts | `/accounts/investments` | redirect |
| `/loans` | Accounts | `/accounts?group=loans` | redirect |
| `/loans/$loanId` | Accounts | `/accounts/<account_id>?tab=schedule` | data redirect |
| `/assets` | Accounts | `/accounts?group=property` | redirect |
| `/assets/$assetId` | Accounts | `/accounts/<account_id>?tab=valuations` | data redirect |
| `/plan` (new) | Plan | `/plan/subscriptions` | redirect |
| `/subscriptions` | Plan | `/plan/subscriptions` | redirect |
| `/subscriptions/$id` | Plan | `/plan/subscriptions/$id` | redirect |
| `/budgets` | Plan | `/plan/budgets` | redirect |
| `/budgets/$budgetId` | Plan | `/plan/budgets/$budgetId` | redirect |
| `/goals` | Plan | `/plan/goals` | redirect |
| `/calendar` | Plan | `/plan/upcoming` | redirect |
| `/reports` | Reports | `/reports` | same; `?tab=` kept for its four views |
| `/net-worth` | Reports | `/reports/net-worth` | redirect |
| `/tax` | Reports | `/reports/tax` | redirect |
| `/tax/$year` | Reports | `/reports/tax/$year` | redirect |
| `/tax/mapping` | Settings | `/settings/categories?tab=tax` | redirect |
| `/rules` | Settings | `/settings/rules` | redirect |
| `/rules/new` | Settings | `/settings/rules/new` | redirect |
| `/rules/$ruleId` | Settings | `/settings/rules/$ruleId` | redirect |
| `/documents/learned` | Settings | `/settings/learned` | redirect |
| `/settings`, `/settings/{profile, security, household, general, exchange-rates, access, categories, intake, ai, data, notifications, users}` | Settings | same | same |
| `/login`, `/login/mfa`, `/login/enroll-mfa`, `/login/link-oidc`, `/forgot-password`, `/reset-password`, `/setup` | none (bare pages) | same | same; fixed by the backend |

Before Start, the plan's Task 5 enumerates the old routes from the
`routes.tsx` files at the branch head, not from this table, and fails if
the two disagree.

- A **data redirect** shows a spinner while it reads `GET /loans/{id}` or
  `GET /assets/{id}`, then replaces the URL. An id that is gone gets "This
  loan no longer exists" (or asset) with a link to Accounts. It never gets
  a blank page.
- **Detail URLs keep their ids.** No id changes meaning.
- **Static paths win over parameters.** `/accounts/investments`,
  `/documents/library`, `/documents/learned`, `/tax/mapping` and
  `/rules/new` are matched before `$id`, as today.
- A client route still never starts with `/api` or `/static`.
  `assetsDir: "static"` stays: `/assets` is still a route, now a redirect.

**One module drives everything.** `src/app/routeMap.ts` holds the six
sections, their sub-pages, and the old-to-new table above. The rail, the
tab bars, the command palette, the redirects and the redirect test all
read it, so they cannot drift apart.

**One module builds the tree.** `src/app/routeTree.tsx` replaces the
per-feature `routes.tsx` files, except the bare `auth` and `setup` pages.
It mounts each section's pages under a section layout route, and loads them
lazily. Reason: the sections now cut across feature folders (Plan holds
four), so a page is registered where its section is. The feature folders
keep their names; `docs/DEVELOPMENT.md` "Plug-in points" changes with it.

### 1.3 Each section

**Home** (`/`): a fixed page, section 3.

**Inbox** has three sub-pages: **Needs you** (`/inbox`), **Documents**
(`/inbox/documents`, the library) and **History** (`/inbox/history`).

- **Needs you** has an Upload button and a drop zone at the top, then the
  cards (section 4), then "Imported recently".
- **Document detail and the mapping editor** sit under the library's path,
  with breadcrumbs ("Inbox / Documents / file name").
- **History** lists answered and dismissed review items, which today sit
  behind the Review page's "Show" select.

**Transactions** (`/transactions`) has no sub-pages. It is one list of
every account.

- **Every filter lives in the URL** and is written back as it changes:
  `account`, `category` (an id, or `none`), `suggested`, `merchant`,
  `subscription`, `document`, `date_from`, `date_to`, `q`, plus `tx`, the
  row whose drawer is open. Clicking "Transactions" in the rail clears
  them.
- **A total row** shows money in, money out and the net of the filtered
  rows, from the rows loaded, labelled "of the N shown". It appears when
  any filter is set.
- **A row opens the drawer** (section 7).

**Accounts** has two sub-pages: **All accounts** (`/accounts`) and
**Investments** (`/accounts/investments`).

- **All accounts**:
  - The header shows the net-worth figure, its change this month and a
    link "Net worth over time" to Reports.
  - Below it the accounts are grouped:
    - Cash: checking, savings, cash.
    - Cards and credit: credit card, line of credit.
    - Investments: brokerage, retirement.
    - Loans: loan, mortgage, manual liability.
    - Property and other: manual asset.
  - Each group shows its total. Each row shows its name, a line saying how
    current it is ("Balance on Nov 12" from `balance_date`; "Your
    estimate, June 2026" for a manual account) and its balance.
  - Closed accounts fold away under "Closed (n)".
  - "Add account" creates loans and assets in the same dialog. Loans
    already work this way (rc.8); assets gain it here.
- **Investments** is today's Investments page: holdings, performance,
  activity and prices across accounts, with the account filter, its tab in
  `?tab=`.
- **The account hub** (`/accounts/$accountId`) has a header with the name,
  type, balance, freshness and actions. Its tabs (`?tab=`) depend on the
  account type:

  | Type | Tabs |
  |---|---|
  | every type | Overview (balance chart and recorded balances), Transactions (the latest 50, each opening the drawer at `/transactions?account=…&tx=…`; "All transactions of this account" link), Documents (`GET /documents?account=`) |
  | brokerage, retirement | + Holdings, Activity, Performance (the investment panels with the account fixed; the cost-basis method card on Holdings) |
  | loan, mortgage | + Schedule, Payments, Rate changes, Payoff (the simulator) |
  | manual asset | + Valuations |

  Today's loan and asset detail pages become these tabs, so a loan has one
  page. Deleting the loan on the Schedule tab keeps the account, as today,
  and the dialog says so. Deleting the account stays in the header's menu.

**Plan** has four sub-pages: **Subscriptions** (`/plan/subscriptions`,
stage 2's view; a series opens as `/plan/subscriptions/$id`), **Budgets**
(`/plan/budgets`; a budget opens as `/plan/budgets/$budgetId`), **Goals**
(`/plan/goals`) and **Upcoming** (`/plan/upcoming`).

- **Upcoming** is today's Calendar: the cash-flow calendar and the
  recurring items list, as `?tab=calendar|recurring`. It is the home of
  recurring income and of items that are neither subscriptions nor bills.
- `/plan` opens Subscriptions, the mock-up's first tab.

**Reports** has six sub-pages: **Spending**, **Income**, **Cash flow** and
**Trends** (`/reports?tab=…`), **Net worth** (`/reports/net-worth`) and
**Tax** (`/reports/tax`; a year opens as `/reports/tax/$year`).

- **Spending, Income, Cash flow and Trends** keep today's control bar and
  state in the address. Their tab links keep the period, account and
  pending choices.
- **Net worth** is today's page. Its breakdown rows now link to their
  account hubs (audit finding).
- **Tax** is today's tax-year list and year pages.

**Settings** (`/settings`) is a side list in six groups. On a phone, the
`/settings` index is the list.

| Group | Pages |
|---|---|
| You | Profile, Security, Notifications |
| Household | Name and currency (`/settings/general`), Members (`/settings/household`), Account access, Exchange rates |
| Categorisation | Categories (tabs: Categories, Payees, Tags, Tax hints), Rules (`/settings/rules`, `/settings/rules/new`, `/settings/rules/$ruleId`), What PerFin learned (`/settings/learned`) |
| Import | Intake |
| Data and integrations | Export and import (`/settings/data`), AI access |
| Instance | Users (instance administrators only) |

- **The two pages called "Household" get distinct names**: "Name and
  currency" and "Members".
- **The Settings intro** reads: "Rules and categories are made where you
  work, in Transactions and the Inbox. They are kept here to edit or
  remove."
- **The tax mapping page becomes the Tax hints tab** of Categories. Tax
  treatment then has one home next to the category it belongs to.

### 1.4 What the mock-up leaves out, and where it goes

| Thing | Home |
|---|---|
| Reports' Cash flow and Trends views | Reports sub-pages, unchanged in content |
| The cash-flow calendar; recurring income and other recurring items | Plan, Upcoming |
| Investment prices and securities (edit, delete) | Accounts, Investments, Prices tab |
| Cost-basis method | account hub, Holdings tab |
| Loan simulator | account hub, Payoff tab |
| Document library, document detail, mapping editor | Inbox, Documents |
| "What PerFin learned" (layouts, wording, mappings, categories) | Settings, Categorisation |
| Exchange rates | Settings, Household |
| Rule export and import | Settings, Rules (unchanged) |
| Dashboard widgets for budgets, goals, holdings, loans, tax documents | not on Home. They are one click away in Plan, Accounts and Reports; missing tax documents stay on the tax year page. |
| Dashboard customisation | removed (non-goals) |
| Header: brand, account menu, household switcher, sign-out, theme toggle | rail top and foot; on a phone, a top bar |
| AI banner ("an assistant has access") | top of the content area, unchanged |
| Settings and theme on a phone (the mock-up hides them) | the phone top bar: account menu (with Settings) and theme toggle |

### 1.5 Where the mock-up and stage 2 disagree, stage 2 wins

- **"Do the same for all …" is unticked**, not ticked as in the mock-up.
  It shows the server's counts. Changing existing rows is a second,
  unticked choice. Section 7 says how.
- **A suggestion looks like a question** ("Groceries?") with Accept. It
  never looks like a chosen category.
- **The descriptor the bank printed stays visible** beside the merchant
  name.

## 2. Look: tokens, type, theming

### 2.1 Tokens

Tokens stay CSS custom properties in `src/app/styles/tokens.css`, and keep
their current names (`--color-*`, `--space-*`, `--radius-*`,
`--font-*`). Only their values change, to the mock-up's palette, and new
tokens are added. Reason: about 2,700 lines of feature CSS use these names.
Renaming would make every lane edit every stylesheet at once.

| Token | Light | Dark |
|---|---|---|
| `--color-bg` | `#f3f6f5` | `#0e1614` |
| `--color-surface`, `--color-surface-raised` | `#ffffff` | `#16211e`, `#1a2623` |
| `--color-surface-sunken` | `#e9eeec` | `#1d2a27` |
| `--color-text` | `#13211d` | `#e7efec` |
| `--color-text-muted` | `#5d6f69` | `#93a59f` |
| `--color-border` | `#d9e1de` | `#2a3a36` |
| `--color-border-strong` | `#7f908a` | `#62756f` |
| `--color-accent`, `--color-focus`, `--color-success`, `--color-positive` | `#0b7a5c` | `#3fc79d` |
| `--color-accent-hover` | `#096a50` | `#5fd4ae` |
| `--color-accent-contrast` | `#ffffff` | `#06231a` |
| `--color-accent-soft`, `--color-success-soft` | `#dff1ea` | `#173a30` |
| `--color-warning` / `-soft` | `#9a5500` / `#fbecd3` | `#f0b35c` / `#3a2c14` |
| `--color-danger` / `-soft` / `-hover` | `#b3261e` / `#fbe3e1` / `#931f18` | `#ff8a80` / `#3d1c1a` / `#ffa49c` |
| `--color-info` / `-soft` | `#2b5fb3` / `#e1ebfa` | `#8db4f5` / `#18294a` |
| `--color-negative` | `var(--color-text)` | `var(--color-text)` |
| `--color-series-1` … `-6` (new) | `#0b7a5c #3f6fd8 #c2772b #8a55b8 #c2496f #6b7f79` | `#3fc79d #7da2f2 #e3a05a #b990e0 #e584a3 #8a9c96` |

There are three deliberate departures from the mock-up, all for contrast:

- **Light warning darkens** from `#a35a00` to `#9a5500`. The mock-up's
  value gives 4.49:1 on its soft background; the new one gives 4.91:1.
- **Input and control borders use `--color-border-strong`**, at 3.09:1 or
  more. WCAG 1.4.11 needs 3:1 for a control's boundary; the mock-up's
  `#d9e1de` line (1.33:1) stays for dividers only.
- **Money out is no longer red.** Amounts out are plain text with a minus
  sign, and amounts in are accent green with a plus, as in the mock-up.
  Red now means only "over" or "error".

New non-colour tokens:

- **Fonts**: `--font-display` ("Bricolage Grotesque Variable", then
  `--font-sans`) and `--font-sans` ("Figtree Variable", then the system
  stack).
- **Type sizes**: `--font-size-3xl: 1.75rem` (h1) and
  `--font-size-display: 2.375rem` (headline money).
- **Layout**: `--rail-width: 13.5rem` and `--content-max: 65rem`.
- **Radii**: `--radius-md: 9px` and `--radius-lg: 12px`.
- **Elevation**: `--shadow-drawer`.
- **Numbers**: `--num: tabular-nums`. Every amount uses
  `font-variant-numeric: tabular-nums` through the `MoneyText` and `.num`
  classes.

**The dark values are written twice**, as today: once under
`@media (prefers-color-scheme: dark) :root:not([data-theme="light"])` and
once under `:root[data-theme="dark"]`. A test fails if the two blocks
differ.

### 2.2 Fonts

**The fonts are self-hosted.** The mock-up loads Google Fonts; the CSP
(`font-src 'self'`) and the no-third-party rule forbid that. Two packages
ship the fonts:

- `@fontsource-variable/figtree` and
  `@fontsource-variable/bricolage-grotesque`.
- Both OFL-1.1, which allows bundling with software under any licence.
- Both pinned exactly at `5.3.0`.
- Both are CSS with `woff2` files and no JavaScript.

Vite emits the files under `/static/` with hashed names. `unicode-range`
means only the Latin files load.

**These are the only new runtime dependencies.** They are justified
because the type is half of the approved look, and system fonts change it
on every platform. If the two together go over the font budget
(section 10), Bricolage is dropped and headings use Figtree 700.

### 2.3 Theming

**The mechanism stays.** `theme.ts`, `ThemeProvider`, the
system/light/dark preference in `localStorage` and the attribute set
before first paint from `main.tsx` are all unchanged.

- **The toggle cycles** System, Light, Dark, as today. Its label names the
  current choice.
- **What changes is the browser chrome**: the `theme-color` metas in
  `index.html` (`#f3f6f5` and `#0e1614`) and the manifest's `theme_color`
  (`#0b7a5c`) and `background_color` (`#f3f6f5`).
- **Charts read the series tokens** through CSS classes, never through
  colours in code. Recharts elements get `className`s such as
  `chart-series-1`, and those classes set `fill` and `stroke` in CSS.

### 2.4 Styling rules (CSP)

The CSP is `style-src 'self'`.

- **CSS ships as files** imported by the component that uses them.
- **No `style` prop, no inline style, no runtime style injection, no
  CSS-in-JS.** After stage 3 the JSX has no `style` prop at all. ESLint
  enforces this with the core rule `no-restricted-syntax` on
  `JSXAttribute[name.name='style']`, so no new plugin is needed.
- **Geometry that comes from data uses SVG attributes.** That covers bar
  widths, stacked bars, columns, sparklines and the page viewer's word
  boxes. SVG `width`, `height`, `x`, `y` and `d` are attributes, not
  styles.
- **The eleven current `style` sites are removed** by the lanes that own
  them. They are listed in the plan.
- **The one exception: `components/Menu.tsx`** may write
  `element.style.top/left` through the CSSOM to place a floating menu, as
  `RowActions` does today. CSSOM writes are not blocked by the CSP, and
  this is the only place that does it.
- **A category's colour** comes from `lib/categoryColour.ts`. It maps a
  top-level category to one of six classes (`chip--s1` … `chip--s6`) by
  its position in the default tree, or by a stable hash of its id for a
  household's own categories.

## 3. Home

Home is a fixed page; nothing on it can be hidden or moved. In order:

1. **Header.**
   - "Good morning", "Good afternoon" or "Good evening", by the browser's
     clock, with the person's display name when known.
   - One line: "Your money at a glance."
   - A primary "Add documents" link to `/inbox`.
2. **Net worth** (left) and **Needs you** (right).
   - **Net worth**:
     - The figure, as headline money.
     - Its change since the start of the month.
     - A 12-month sparkline from `GET /networth` (`GET /networth/latest` for the figure), drawn in SVG
       with an `aria-label` that names the first and last values.
     - A link to Accounts.
   - **Needs you**: the first three Inbox cards as one-line rows (file
     name and the card's question, or the subscription change), each
     linking to its card. Then, when there are uncategorised or suggested
     transactions, one row: "N transactions have no category" (and "M
     have a suggestion" when there are some). It links to `/transactions?category=none`. Then
     "Open Inbox". Empty: "Nothing is waiting. Every document is
     imported."
3. **Spending this month** (left) and **Coming up** (right).
   - **Spending this month**:
     - The month's spending.
     - Its comparison with the same day of last month ("8% less than this
       point in October"), from two `GET /reports/spending` calls.
     - A stacked bar of the five largest top-level categories plus
       "Other", with a legend of amounts.
     - A link to Reports.
   - **Coming up**: the next five charges in 30 days from
     `GET /subscriptions` and `GET /recurring`, with name, date and amount.
     - A note when a price changed ("went up $2.00 last month") or the
       amount varies ("usually $105 to $125", amount shown as "~$118").
     - A link to Plan.
4. **Latest transactions**: five rows, each opening the drawer at
   `/transactions?tx=…`, and a link "All transactions".
5. **"Start here"** replaces sections 2 to 4 for a household with no
   accounts (today's `GettingStarted`).

Each panel loads and fails on its own. A failed panel says what failed in
one line, with Retry.

## 4. Inbox

### 4.1 What is a card

Each card comes from data that exists today. The Inbox invents no second
source of truth.

| Card | Source | Actions |
|---|---|---|
| **Document**: one per document with an open review item | `GET /review/cards` (stage 1's `DocumentCard`, with the reconcile view and every review tool) | the card's next action, as today; Resolve expands the card in place |
| **Loose item**: an open review item of no document | `ReviewCardPage.loose` | as today |
| **Subscription**: one card per series that is suggested or has open changes (price changed, missed, ended, charged after end, trial became paid, renews soon); the card lists each open change | `GET /subscriptions?status=suggested`, `GET /subscriptions/events?state=open` | Confirm, "Not a subscription", "Got it" (`POST /subscriptions/{id}/decision`, `POST /subscriptions/events/seen`) |
| **Suggested categories**: one card, when there are suggestions | `GET /categorisation/summary`, `GET /transactions?suggested=true&limit=200` | up to five merchants, each with its count and "Accept N"; "Review all in Transactions" |
| **Make it a rule**: one card per learned entry offered as a rule (stage 2's `offer_rule`), at most three | `GET /categorisation/learned` (first page) | "Always…" opens the drawer at `/transactions?always=<example id>` (stage 2's link, kept); "Not now" hides it for the session |

- **"Accept N"** is one bulk `accept_suggestions` call with the loaded ids
  of that merchant. That is a person's choice (`person`).
- **When more than 200 suggestions exist**, the card says "the five most
  common of N".

**Uncategorised rows are not a card.** They are a line under the
suggestions card ("N have no category" with a link to
`/transactions?category=none`). Reason: a large number of them is the
normal state of an old ledger, and a card that never goes away teaches
people to ignore the Inbox.

### 4.2 Order

1. **Documents whose money is wrong until someone answers**: actions
   `reconcile`, `check_rows`, `confirm_duplicate`, `answer_sign`.
2. **Documents that cannot import until someone answers**:
   `choose_account`, `map_columns`, `choose_owner`, `teach`, `reprocess`.
3. **Documents with something to note**: `attach_receipt`,
   `acknowledge`, and an action the client does not know.
4. **Loose items.**
5. **Subscription cards**, one per series: series with an open change
   first (by their newest change, newest first), then suggested series
   with no change (most charges first).
6. **Suggested categories.**
7. **Make it a rule.**

Inside groups 1 to 3, the oldest document (`created_at`) comes first.
`inbox/order.ts` holds the order, and a unit test pins it.

**A card that was answered stays in place** until the page is left. It
says what happened ("Imported 42 rows into Everyday Checking", with links),
as the Review page does today with `SettledCard`.

### 4.3 The rest of the page

- **Imported recently**: the last 10 documents finished in the last 14
  days, processed or failed (`GET /documents?limit=25`, filtered on the
  client). Each row has its file name (a link), account
  (a link), period and, when the read was not checked against a
  statement balance (`unchecked`), the quiet marker "Not checked against a
  balance". A file that failed says so.
- **Empty Inbox**: "All clear. Every document you added is imported. Drop
  new statements here any time." The upload zone stays.
- **`?document=<id>`** shows only that document's card, with "Show
  everything", as `/review?document=` does today.

### 4.4 The count

- **The badge comes from one new endpoint**, `GET /api/v1/inbox/summary`
  (section 11). It is cached under the query root `inbox`.
- **The live-update stream invalidates it** on `document.*`, `review.*`,
  `subscription.*`, `schedule.*` and `transaction.*`.
- **The badge counts the cards that need an answer**: documents, loose
  items, subscription series and the suggestions card. "Make it a rule"
  cards are offers, not questions, and are not counted. The badge's
  accessible name is "N things need you". On a request error it shows
  nothing, not zero.
- **Loose items** are shown up to the review service's `LOOSE_MAX`; the
  count is exact, and when it is larger the list ends with "N more not
  shown".

## 5. Stage 2 components, rehoused

| Stage 2 piece | Stage 3 home |
|---|---|
| `features/subscriptions/SubscriptionsPage` and `SubscriptionDetail` | Plan > Subscriptions at `/plan/subscriptions[/$id]`. The figures become a `StatGroup`; the table becomes cards under 720 px as today. Its "Needs a look" section stays, and the same series also appear as Inbox cards. |
| `transactions/provenance.ts` (source and confidence) | the grid's category cell (a chip, with the source and confidence in small text under it) and the drawer's "Category by" line |
| Suggestion with Accept in the grid | unchanged in the grid; also in the drawer |
| `AlwaysCategoriseDialog` | its form becomes the drawer's "Always use … for …" section. The dialog stays for the row menu; `?always=<id>` now opens the drawer for that row with the box ticked and the preview shown, because the person asked for it; nothing is saved before Save, and changing existing rows stays unticked. |
| `TeachPrompt` | removed: the drawer's box is shown at the moment of the change |
| `components/CategoryPicker` | restyled; gains an optional "New category…" entry (section 7) |
| `imports/LearnedPage` with the categorisation cards | `/settings/learned` |
| Rules: the merchant condition and Undo | `/settings/rules` |
| `calendar/RecurringTab` with kind and status | Plan > Upcoming, Recurring items tab |
| The four subscription notification kinds | Settings > Notifications, unchanged |
| `features/subscriptions/useWide.ts` | promoted to `lib/useMediaQuery.ts`; the subscriptions lane switches to it |

## 6. Components

**New, in `src/components/`.** Each gets its own test file.

- `Drawer`: a right-hand panel with a `role="dialog"`, a label, focus
  trap, Escape and focus return. It is a full-screen sheet under 820 px.
- `Menu`: promoted from `transactions/RowActions`, following the WAI-ARIA
  menu button pattern.
- `SectionNav`: the tab bar of a section, made of links with
  `aria-current`. Its items come from `routeMap.ts`, and an item may keep
  named search parameters.
- `PageHeader`: title, one-line description, actions and breadcrumbs. It
  renders an `h1`, or an `h2` inside a section layout, which already
  renders the `h1`.
- `Breadcrumbs`.
- `StatGroup` and `Stat`: a label, a figure and an optional change.
- `Chip`: a category chip with a colour dot class, a toggle chip with
  `aria-pressed`, and the tones neutral, ok, warn, bad and info.
- `ProgressBar`: in SVG, with `role="progressbar"`, `aria-valuenow`,
  `aria-valuemax`, and an over-limit tone.
- `StackBar`: an SVG stacked bar with `role="img"`, an `aria-label` and a
  legend list beside it.
- `Sparkline`: an SVG path with `role="img"` and an `aria-label`.
- `QuietNote`: the muted dot-and-text marker for "not checked" and
  "estimate".
- `features/categories/NewCategoryDialog`: name and parent; owned by Start
  because two lanes use it.

**Restyled, with their API kept**: Button, Card (it becomes the mock-up's
panel), Badge, Tabs (gains a URL-synced mode through
`lib/urlState.ts::useTabParam`), Table (adds `rowLink` for a focusable
row that opens something), Dialog, Toast, Field, Input, Select, Checkbox,
EmptyState, Spinner, MoneyText, IconButton, CategoryPicker.

**Icons**: `icons.tsx` gains Home, Inbox, List, Card, Target, Bars, Gear,
Search, More, ChevronRight, Upload and Check. They are inline SVG
components with `stroke="currentColor"`, as today.

**New in `src/lib/`**: `urlState.ts` (read and write search parameters
with `replace`, defaults left out, unknown values ignored; lifted from
`reports/searchState.ts`), `useMediaQuery.ts`, `categoryColour.ts`.

**Deleted**:

- The placeholder mechanism: `app/shellRoutes.tsx`, `PlaceholderPage`, and
  `LoginPage` in `app/pages.tsx`.
- The header and sidebar CSS in `layout.css`, which is rewritten.
- The dashboard's `layout.ts`, `useDashboardLayout.ts`, the customise
  dialog and the widget catalogue.
- `transactions/TeachPrompt.tsx`.
- `features/subscriptions/useWide.ts`.
- The inline select in the grid's category cell. Changing a category moves
  to the drawer and the bulk bar.

## 7. Transactions: the drawer and making rules in context

Clicking a row, or pressing Enter on a focused row, opens the drawer and
sets `?tx=<id>`. Closing removes it. Back closes it.

The drawer shows, in order:

- the merchant (the display name), the amount as headline money, and the
  date;
- the account (a link to its hub);
- **"Bank wrote"**: the raw descriptor, always shown;
- **"Came from"**: a link to `/inbox/documents/<source_document_id>`, or
  "Entered by hand";
- **"Category by"**: stage 2's provenance sentence and confidence, such as
  "Merchant list, 90%" or "You chose this".

**Category** is a `CategoryPicker`, preset to the row's category or its
suggestion. A suggestion shows "Groceries?" with Accept.

- **Choosing a category** is a person's decision (`PATCH`, optimistic).
  It is undoable from the toast.
- **Under the picker, for a teachable row**, there is a checkbox,
  **unticked**: "Always use <category> for <merchant>".
  - Ticking it fetches the server preview (`POST /rules/merchant/preview`)
    and shows its sentences: "17 without a category", "5 that PerFin
    guessed differently will change", "6 you or a rule chose stay as they
    are".
  - It then shows a second checkbox, also unticked: "Also change the 22
    existing transactions".
  - Save creates the merchant rule (`POST /rules/merchant`), with
    `apply_to_existing` set only when the second box is ticked.
  - The toast carries Undo (revert).
  - When the row cannot teach, or an earlier rule already decides the
    merchant, the box is replaced by stage 2's reason and a link to that
    rule.
- **"New category…"** at the end of the picker opens `NewCategoryDialog`
  (name, parent preset to the current group). The new category is then
  chosen. It is offered to members and above (`POST /categories` needs
  member); viewers do not see the entry.
- **Edit details** opens today's `TransactionDialog` for amount, dates,
  payee, memo, tags and splits. Delete and "Mark as transfer" sit in the
  drawer's menu.

**Other places that teach in context:**

- **Inbox**: "Accept N" on the suggestions card, and "Always…" on "Make it
  a rule" (section 4).
- **Settings > Rules and Categories** list, edit, reorder and remove. They
  say on top that rules are made where you work.

**The trust principle on screen:**

- A guessed category always shows its source.
- A suggestion never looks like a fact.
- An unchecked import carries "Not checked against a balance" on the
  Inbox and on the document page.
- No screen offers a way to overwrite a row a person decided, except that
  person choosing again.

## 8. Keyboard and accessibility

The target is WCAG 2.2 AA.

**Keyboard:**

- **Skip link**, as today.
- **Ctrl+K / Cmd+K opens the command palette.** It has a combobox and a
  listbox and goes to any section, sub-page, settings page or account (by
  name, from the accounts already loaded). It does not search
  transactions.
- The Transactions search field is the first control after the heading;
  there is no character-key shortcut for it.
- **Escape closes** the drawer, a dialog or the palette.
- **In the Transactions list**, Up and Down move between rows (roving
  `tabindex`) and Enter opens the drawer.
- **No character-key shortcuts at all**, so WCAG 2.1.4 has nothing to
  turn off; every shortcut needs Ctrl or Cmd, or acts only on the focused
  list (the arrow keys).

**Accessibility rules:**

- **Landmarks**: the rail is `nav` "Sections", a tab bar is `nav` named
  after its section, and the content is `main`. Each page has exactly one
  `h1`.
- **Focus not obscured (2.4.11)**: `main` has
  `scroll-padding-bottom` equal to the phone bottom bar's height. Toasts
  sit above the bar. The drawer traps focus.
- **Target size (2.5.8)**: chips, the rail on a phone and icon buttons
  are at least 24 by 24 CSS pixels.
- **A clickable row is never only a click handler.** The row's first
  cell holds a real button or link that does the same, and the row
  handler only forwards to it.
- **Contrast**: 4.5:1 for text, 3:1 for large text, control boundaries,
  focus rings and chart series against the surface. A token test checks
  this, because axe cannot judge contrast in jsdom.
- **Motion**: the drawer slides in only under
  `prefers-reduced-motion: no-preference`.
- **Charts**: every chart has a table or list with the same figures beside
  it (Reports already does this) or an `aria-label` that gives them
  (sparkline, stacked bar).
- **Live regions**: the Inbox badge text is not live, so it does not
  interrupt. Toasts are `role="status"`, as today.

## 9. Responsive

| Width | Layout |
|---|---|
| 1024 px and wider | rail of 13.5rem with labels; content up to 65rem, centred |
| 820 to 1023 px | icon rail of 4.5rem. Labels are visually hidden but stay the links' names, and the badge is a dot with the count in the name. |
| under 820 px | a top bar (brand, account menu, theme toggle) and a bottom bar with the six sections. The bottom bar keeps the safe-area insets. Settings sits in the account menu. The drawer becomes a sheet. Two-column panel grids become one column. |
| under 720 px | tables that have a card layout switch to it (subscriptions, accounts, Inbox recent, Transactions: date, merchant, amount, then category on a second line) |

These widths are constants in CSS and in `lib/useMediaQuery.ts`, never in
CSS variables, because media queries cannot read variables. No feature
exists only on one width.

## 10. Performance budget

**Routes are split**: each section's pages load through TanStack Router's
`lazyRouteComponent`.

- **Loaded at once**: the shell, Home and Transactions.
- **Loaded on demand**: Inbox and the review tools, the page viewer, the
  mapping editor, Accounts and investments, loans, Plan, Reports with
  Recharts, Settings, and AI access.

| Measure | Budget |
|---|---|
| JS needed for `/` (entry and its static imports), gzip | at most 250 kB |
| Largest lazy chunk, gzip | at most 160 kB |
| All JS, gzip | at most the Start baseline plus 5% |
| All CSS, gzip | at most 20 kB |
| Font files a Latin page loads (`woff2`) | at most 120 kB |

**`frontend/scripts/check-bundle.mjs` checks these after `npm run
build`.**

- It reads `dist/index.html` for the entry script, its preloads and the
  stylesheets, gzips each file and sums them. It needs no Vite manifest.
- It fails with the figures. The budgets are set before measurement: an
  overrun found during the stage is reported to the coordinator with the
  figures, who decides between a fix and a changed budget; a lane never
  raises a budget itself.
- `make frontend-check` and CI run it.
- Start records the baseline after stage 2 in the script's header. The
  rc.8 figure (377 kB gzip, one chunk) predates stage 2 and is not the
  baseline.

## 11. Backend

**One read-only endpoint**, needed for the rail badge:

- **The route**: `GET /api/v1/inbox/summary`. Scope `review:read`, role
  viewer.
- **Its response, `InboxSummary`**:
  - `documents: int`: documents with an open review item the caller may
    see, uncapped.
  - `loose: int`: open items of no document.
  - `subscriptions: int`: series that are suggested or have an open
    change, each counted once.
  - `suggestions: int` and `uncategorised: int`: transactions on visible
    accounts.
  - `cards: int`: `documents + loose + subscriptions + (1 if suggestions
    else 0)`.
- **Code**: `app/domain/inbox/` (`schemas.py`, `service.py`) and
  `app/api/routers/inbox.py`, which is registered automatically.
- **The service only reuses** the review visibility filter, the
  categorisation summary's counts, the subscription list's suggested
  series and the open events. It does not compute the learned list, so a
  live update stays cheap.

**Why one endpoint and not one field on `ReviewCardPage`**: the badge is
on every page. Built from today's endpoints it would cost four requests on
every load and live update (the review cards paged through to count, the
subscription list, its events, the categorisation summary). The aggregate
is one cheap request with exact counts.

**Gates:**

- One line in `tests/security/authz_policy.py`.
- An integration test that a viewer sees only counts on accounts they may
  see.
- `openapi.json` and `schema.d.ts` regenerated, and the drift checks
  green.
- No new column, so no classification change.
- The MCP tool count (19) and the AI read API (19 routes) do not change.

Nothing else in the backend changes. Every other number the new screens
show is already in the API (the plan lists each one with its endpoint).

## 12. Testing

- **Test first**, per component and per page.
  - **New primitives**: role, name and state (`components/*.test.tsx`).
  - **Pages**: through `renderApp(path)` at the **new** URL, with the
    routed fetch mock (`features/auth/testing.tsx`). Queries go by role
    and accessible name, as today.
- **Redirect tests** (`app/redirects.test.tsx`): every row of the route
  map's old-to-new table is rendered at the old URL. The test asserts the
  new pathname, the kept search and hash, and that the new page's heading
  is shown.
  - Two data redirects also get a test with a missing id.
  - A completeness test compares the table with the old routes listed at
    Start.
- **Accessibility**: `axe-core` (MPL-2.0) becomes a devDependency, pinned
  exactly at `4.14.1`.
  - It is a test tool, not shipped, so it is not a runtime dependency.
  - `src/test/axe.ts` runs it on a rendered container with `color-contrast`
    off, because jsdom cannot compute colours.
  - Every page test file has one `has no axe violations` case for its
    main state.
  - The route audit (Final) runs axe on every new URL in both themes.
- **Contrast**: `app/styles/contrast.test.ts` parses `tokens.css` and
  checks the pairs listed in section 8 in both themes. It fails with the
  pair and the ratio.
- **Pure logic** gets unit tests: route map, Inbox order, category colour,
  URL state, greeting, spending comparison and the bundle check's parser.
- **The suite stays green through Start.** Old URLs redirect, so the
  roughly 300 `renderApp` calls that use old paths keep passing. Each lane
  moves its own tests to the new URLs.

## 13. Risks

- **Many tests move at once.** About 300 `renderApp` sites, 68 on
  `/review` and 66 on `/transactions`.
  - Mitigation: redirects keep the old paths working, so lanes can move
    tests in their own time.
  - Accessible names stay where the content stays.
  - Start keeps the whole suite green before any lane starts.
- **Lazy routes and synchronous queries.** A test that uses `getBy` right
  after `renderApp` fails once its page loads lazily.
  - Mitigation: Start runs the full suite with lazy routes on. A failing
    test moves to `findBy`, in the file's owning lane, or in Start for
    shared files.
- **The drawer replaces inline category editing.** People who
  categorised with the select in the grid now need one click more.
  - Mitigation: Enter opens the drawer with the picker focused, Escape
    returns to the row, and the bulk bar still sets many rows at once.
  - This decision can be revisited after rc.11.
- **Two h1s while the lanes work.** Section layouts render the `h1`, and
  pages that write their own `h1` lose it in their lane.
  - Mitigation: the Final route audit asserts exactly one `h1` per URL.
- **The fonts may go over budget.** Mitigation: the rule in section 2.2.
- **Viewer write controls.** Budgets, Goals and Categories gain the role
  checks they lack. The server already refuses, so this changes what is
  shown, not what is allowed.
- **Docs cite frontend paths.** `CONTROLS.md` (about 39), `INGESTION.md`,
  `CATEGORISATION.md`, `DEVELOPMENT.md`, `API.md` and `ROADMAP.md` cite
  them.
  - Feature folders keep their names, so most citations hold.
  - The Final docs task fixes routes and the moved files.
