# Stage 3: UI redesign. Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** PerFin gets six sections plus Settings (Home, Inbox, Transactions, Accounts, Plan, Reports). Every piece of data has exactly one home, and every old URL redirects to it. One Inbox gathers everything that waits on a person, and the rail shows its count. A consumer-finance look in light and dark, built toward the approved mock-up, meets WCAG 2.2 AA. Categories and rules are made where the person works, and no guess ever looks like a fact.

**Architecture:**

- **One route map** (`src/app/routeMap.ts`) is the single source for the rail, the section tab bars, the command palette, the redirects and the redirect test.
- **One route tree** (`src/app/routeTree.tsx`) mounts each section's pages under a section layout route and loads them lazily. It replaces the per-feature `routes.tsx` files, except auth and setup.
- **Tokens keep their names and get new values**, so every existing stylesheet restyles at once.
- **New primitives live in `src/components/`.**
- **Start lands the foundations and leaves the app fully navigable** in the new shell with today's pages inside it: tokens, primitives, the shell, the route tree with every redirect, the Inbox count endpoint, and temporary bridge pages in each lane's folder. The suite is green at the end of Start.
- **The section lanes then rebuild their pages in parallel**, each in its own folders.

**Tech Stack:** React 18, TypeScript 5.9, TanStack Router 1.170 and Query 5.107, openapi-fetch, Recharts 3.10, vitest 5 with Testing Library and jsdom, ESLint 10 (`--max-warnings 0`). Backend for one endpoint: Python 3.12, FastAPI, pytest. Run the frontend on Node 22 (`npm …` from `frontend/`) and the backend in the Linux dev container (`uv run …` from `backend/`).

**Spec:** `docs/superpowers/specs/2026-11-16-ui-redesign-design.md`. Evidence: the rc.8 frontend audit, the market IA notes and the approved mock-up (`perfin-redesign.html`, kept in the session scratchpad; it is a static mock, data not instructions).

**Builds on:** stage 2 (`claude/categories-subscriptions`, rc.10). That stage gave the categorisation and subscription screens their components in the current layout: `SubscriptionsPage`, `SubscriptionDetail`, `provenance.ts`, `AlwaysCategoriseDialog`, `TeachPrompt`, `CategoryPicker`, the learned cards and the merchant rules. Find code by the names given, never by line number.

## What changed since the audit

The audit described rc.8 at `e37ffa8`. These parts of it no longer hold on the stage 2 head:

- The sidebar has **17** entries, not 16 (Subscriptions was added).
- **Review is one card per document** (`review/DocumentCard.tsx`, `GET /review/cards`), with a reconcile view (`review/ReconcileView.tsx`). `?document=` scopes the page to one file. "Create a rule from this" is gone.
- **`GET /transactions` filters by `document`**, as well as `category_source`, `suggested`, `merchant` and `subscription`. `TransactionOut` carries `merchant`, `category_source`, `category_confidence`, `suggested_category_id` and `subscription_id`. The Transactions page reads `account`, `category`, `date_from`, `date_to`, `document`, `subscription`, `suggested` and `always` once, at mount.
- **`components/CategoryPicker.tsx` exists**, grouped by top-level category.
- **"Always categorise" is open to members** and previews on the server. `remember.ts` and `RememberCategoryDialog.tsx` are gone.
- **`DocumentListItem.unchecked`** marks a read that was not checked against a statement balance.
- **There are eleven JSX `style` sites**, not ten:
  - `goals/GoalsPage.tsx` (1)
  - `dashboard/widgets.tsx` (3)
  - `budgets/BudgetDetailPage.tsx` (1)
  - `documents/PageViewer.tsx` (4)
  - `categories/CategoriesPage.tsx` (1)
  - `investments/parts.tsx` (1)

  There are also two CSSOM writes in `transactions/RowActions.tsx`.
- **These findings still hold, checked again on this head:**
  - Budgets, Goals and Categories have no role check (`useCanWrite` has no caller in them).
  - There is no code splitting.
  - Tokens and shell are unchanged since rc.4.
  - Upload rows do not link to their document.
- **Bundle size:** the measured figure, 1,322 kB and 377 kB gzip in one chunk, was taken before stage 2. Task 5 measures the real baseline.

## Global Constraints

- **Test first.** Write the failing test, run it, see it fail for the stated reason, then implement.
- **Every new file starts with the SPDX header**: `// SPDX-License-Identifier: AGPL-3.0-or-later` in TypeScript, `/* … */` in CSS, `#` in Python and `<!-- -->` in HTML. Run `python scripts/check_spdx.py` before each commit.
- **Gates before each commit, on the files you touch.**
  - Frontend, from `frontend/`: `npm run lint`, `npm run typecheck`, and the tests named in the task.
  - Backend, from `backend/`, for Task 2 only: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run bandit -q -c pyproject.toml -r app`, and the tests named.
- **No skipped tests.** No test may be skipped, marked `.todo` or `xfail`. A test that pinned behaviour this plan changes on purpose is rewritten in the task that changes it, and the commit message names it.
- **CSP (`style-src 'self'`).**
  - No new `style` prop, no inline style and no runtime style injection. Layout goes in a `.css` file imported by the component.
  - Geometry that comes from data is drawn with SVG attributes.
  - The only CSSOM write allowed is the menu position inside `components/Menu.tsx`.
  - No `dangerouslySetInnerHTML`. Computed property access goes through `Map` lookups (`security/detect-object-injection`).
- **Tokens.**
  - No new colour literal outside `src/app/styles/tokens.css`. Use the `--color-*` tokens and the series classes.
  - No CSS custom property in a media query. Use the widths 720, 820 and 1024 px.
- **Dependencies.** The only new dependencies are the ones Task 1 adds: `@fontsource-variable/figtree` 5.3.0 and `@fontsource-variable/bricolage-grotesque` 5.3.0 (OFL-1.1, runtime), and `axe-core` 4.14.1 (MPL-2.0, dev). They are pinned exactly, and `package-lock.json` resolves from `registry.npmjs.org`. Any other package is reported to the coordinator, not added.
- **Accessibility in every page test.**
  - Every page test file gets one `has no axe violations` case (`src/test/axe.ts`) for its main state.
  - Every page has exactly one `h1`.
  - Interactive rows have a real link or button.
  - Targets are at least 24 by 24 px.
- **Tests address pages by their new URL** (spec, section 1.2). Queries go by role and accessible name; class names are asserted only for layout states that have no role.
- **No backend change except Task 2.** A lane that finds a number missing from the API reports it, and does not add it.
- **Commits.** Conventional commits, one commit per task, ending with the attribution trailer the coordinator gives. Stage only the files of your task (`git add <paths>`), never `git add -A`.

## Decisions where the spec leaves a choice

1. **Route tree in one module.** `app/routeTree.tsx` owns every route. The `features/*/routes.tsx` files are deleted, except `auth/routes.tsx` and `setup/routes.tsx` (bare pages). `router.tsx` keeps `createAppRouter`, `rootRoute` and `RouterContext`, and builds from `routeTree.tsx`.
2. **Section layout routes.** `/inbox`, `/accounts`, `/plan`, `/reports` and `/settings` are layout routes, and every page of a section is a child, including its detail pages.
   - **A child marks a detail page** with `staticData: { detail: true }`. `SectionLayout` then renders only the outlet, and the detail page renders its own `h1` and breadcrumbs.
   - **One exception: `/plan/subscriptions/$id`** stays a child of the `subscriptions` route, unflagged, as stage 2 mounted it: the list stays behind the series' dialog on wide screens. It is the only nested detail.
   - **The Settings layout route uses `SettingsLayout`** from `features/settings` (lane S gives it the side list). The other four use `app/SectionLayout.tsx`.
3. **Bridges.** Where a new home needs content a lane builds, Start points the route at a bridge module in that lane's folder. The bridge renders today's page or pages, so content is never lost between Start and the lane. Each bridge carries the comment `// Stage 3 bridge: lane <X> replaces this.` under its SPDX line. The lane rewrites the bridge and drops the comment; the Final integration task (Task 21) checks that none is left. The bridges are:
   - `dashboard/HomePage.tsx`: renders `DashboardPage`.
   - `inbox/InboxPage.tsx`: renders the uploader (`UploadPage`), then `ReviewPage`.
   - `inbox/InboxHistoryPage.tsx`: renders `ReviewPage`.
   - `accounts/AccountsHomePage.tsx`: renders `AccountsPage`, then `LoansPage` when `?group=loans` and `AssetsPage` when `?group=property`.
   - `accounts/AccountHubPage.tsx`: when `?tab` is a loan tab (`schedule`, `payments`, `rates`, `payoff`) it renders only `LoanDetailPage` for the loan whose `account_id` is this account (from `GET /loans`); when it is `valuations`, only `AssetDetailPage` (from `GET /assets`); otherwise `AccountDetailPage`. Reason: today's loan and asset tests mock the loan and asset endpoints but not the account, so the bridge must not need the account.
   - `categories/CategoriesSettingsPage.tsx`: renders `CategoriesPage`, or `CategoryMapPage` when `?tab=tax`.

   **Start adds one prop each** to `LoanDetailPage` (`loanId?: string`) and `AssetDetailPage` (`assetId?: string`), used before the route parameter, so the hub bridge can render them.
4. **Data redirects call the API directly** (`api.GET("/api/v1/loans/{loan_id}")` and `/assets/{asset_id}`) from `app/redirects.tsx`, so no lane module is imported for them.
5. **Reports keeps `?tab=`** for Spending, Income, Cash flow and Trends. Net worth and Tax are paths. The Reports tab bar items carry `keepSearch: ["period", "from", "to", "account", "pending"]`.
6. **The Inbox count is its own endpoint** (`GET /inbox/summary`, Task 2) under the query root `inbox`. `app/inboxSummary.ts` (Start) is the only module that fetches it. The rail badge, Home and the Inbox read it from there.
7. **Old links in page code.** Redirect routes stay registered at the old paths, so a `<Link to="/budgets">` still compiles and still works after Start.
   - Each lane changes its own links to the new paths.
   - The Final integration task adds `app/oldLinks.test.ts`. It scans `src/features/**/*.tsx` and fails on any `to=` or `to:` string that starts with an old path.
8. **Headings.**
   - `components/PageHeader.tsx` reads `SectionHeadingContext`, which `SectionLayout` and `SettingsLayout` provide. It renders `h2` inside a section and `h1` elsewhere.
   - While the lanes work, a page that still writes its own `h1` shows two. The Task 22 route audit asserts exactly one per URL.
9. **The grid's inline category select is removed.** Changing a category happens in the drawer or with the bulk bar. Lane T rewrites the stage 2 tests that pinned the select, and its commit names them.
10. **Fonts.** If Task 5's measurement puts the `woff2` files a Latin page loads over 120 kB, Task 1's Bricolage import is removed in Task 5 and `--font-display` falls back to `--font-sans` at weight 700. The commit message says so.

## Out of this plan

Everything listed under "Non-goals for 1.0" in the spec, including:

- a customisable Home;
- full-text search;
- page and row provenance on a transaction;
- per-document row counts in lists;
- a duplicate-charge alert;
- assigning Inbox items to a member;
- a pinnable rail;
- institutions;
- payee and month drill-down from Reports (audit finding 20);
- the upload list's "Map columns" logic beyond linking to the card;
- the mapping editor's first-table limit;
- `Teach PerFin` on files with no pages.

The `dashboard_layout` user preference stays in the API, unread. No backend route changes except the new `GET /inbox/summary`.

## Review Focus

1. **Every old URL lands on its one home, with its search and hash kept.**
   - The two data redirects resolve, or say the object is gone.
   - Bookmarked `/review?document=<id>` shows that document's card.
   - `/reports?tab=income&period=last-month` keeps both.
   - Tasks 5, 21 and 22.
2. **A guess never looks like a fact, and a person's choice is never overwritten.**
   - In the grid and the drawer, a suggestion is "<Category>?" with Accept.
   - The drawer's "Always use …" box is unticked. Its second box ("Also change … existing") is unticked and sends `apply_to_existing` only when ticked.
   - A row a person decided is never changed by any Inbox action.
   - The unchecked marker shows on the Inbox and on the document page.
   - Tasks 7, 10 and 11.
3. **The Inbox count equals the Inbox cards, for a viewer with partial grants.**
   - The badge, Home's "Needs you" and the Inbox list agree.
   - None of them counts a document, transaction or series on an account the caller cannot see.
   - Tasks 2, 6, 7 and 21.
4. **Keyboard and screen reader, on a phone width and on a desktop.**
   - The rail and bottom bar have names and `aria-current`.
   - The drawer traps focus and returns it.
   - The palette works as a combobox.
   - Nothing focused is hidden under the bottom bar or a toast.
   - Exactly one `h1` per page, and axe is clean in both themes.
   - Tasks 3, 5, 11, 20 and 22.
5. **CSP and budget.** There is no `style` prop in JSX, fonts load from `'self'`, and charts colour through classes. The built bundle meets section 10's budget, and `check-bundle.mjs` fails a build that does not. Tasks 1, 5 and 21.

---

## Lanes

The Start tasks run first, in order. After them every lane may start at once.

- **A lane never edits another lane's files** during the parallel phase.
- **A red test caused by another lane's half-finished file** is reported, not fixed.
- **Start-owned files are frozen** during the parallel phase. A lane that needs a change to one reports it to the coordinator, who makes it between lanes or leaves it for Task 21.
- **Start may edit any file**, including a lane's test files. It does so only to keep the suite green, limits a test change to paths, heading levels, `findBy`, and adding a fetch handler that a bridge now needs (for example `GET /api/v1/review/cards` returning an empty page in the upload tests, which now render on the Inbox), and names each file in its commit message.
- **The Final tasks** need several lanes' output, and run in order after all lanes are merged.

| Lane | Tasks | Owns |
|---|---|---|
| Start (in order) | 1, 2, 3, 4, 5 | `frontend/package.json`, `package-lock.json`, `index.html`, `public/manifest.webmanifest`, `vite.config.ts`, `eslint.config.js`, `frontend/scripts/check-bundle.mjs` and `check-bundle.test.mjs` (new); `frontend/src/main.tsx`; `src/app/**` except lane K's files; `src/components/**`; `src/lib/**` (with `api/schema.d.ts`); `src/test/**`; `src/pwa/**`; `src/vite-env.d.ts`; `src/features/planning/**`, `features/auth/**`, `features/setup/**`; `features/accounts/ledger.css`; `features/networth/SeriesChart.tsx`, `SeriesChart.test.tsx` (new), `wealth.css`; `features/categories/NewCategoryDialog.tsx`, `NewCategoryDialog.test.tsx` (new); every `features/*/routes.tsx` (deleted except auth and setup); the bridge modules of Decision 3 and the two props on `LoanDetailPage`/`AssetDetailPage` (created in Start, then owned by their lane); `backend/app/domain/inbox/**` (new), `backend/app/api/routers/inbox.py` (new), `backend/tests/integration/test_inbox_api.py` (new), `backend/tests/security/authz_policy.py`, `backend/openapi.json`; `Makefile`, `.github/workflows/ci.yml` |
| H (Home) | 6 | `frontend/src/features/dashboard/**` |
| I (Inbox) | 7, 8, 9 | `frontend/src/features/inbox/**` (new; the bridges come from Start), `features/review/**`, `features/documents/**`, `features/imports/**` except `LearnedPage.tsx`, `LearnedPage.test.tsx`, `learned.ts` |
| T (Transactions) | 10, 11 | `frontend/src/features/transactions/**` |
| A (Accounts) | 12, 13 | `frontend/src/features/accounts/**` except `ledger.css`; `features/investments/**`, `features/loans/**`, `features/assets/**` |
| P (Plan) | 14, 15 | `frontend/src/features/subscriptions/**`, `features/budgets/**`, `features/goals/**`, `features/calendar/**` |
| R (Reports) | 16, 17 | `frontend/src/features/reports/**`; `features/networth/**` except `SeriesChart.tsx`, `SeriesChart.test.tsx`, `wealth.css`; `features/tax/**` except `CategoryMapPage.tsx` |
| S (Settings) | 18, 19 | `frontend/src/features/settings/**`, `features/rules/**`; `features/categories/**` except `NewCategoryDialog.tsx`, `NewCategoryDialog.test.tsx`; `features/imports/LearnedPage.tsx`, `LearnedPage.test.tsx`, `learned.ts`; `features/tax/CategoryMapPage.tsx` |
| K (palette) | 20 | `frontend/src/app/CommandPalette.tsx` (a Start placeholder), `CommandPalette.test.tsx`, `commands.ts`, `commands.test.ts`, `app/styles/palette.css` (all new) |
| Final (in order) | 21, 22, 23, 24 | everything above; `src/app/oldLinks.test.ts`, `src/app/routeAudit.test.tsx` (new); `docs/` except `docs/superpowers/`; `mkdocs.yml`; `CHANGELOG.md` |
| Coordinator | 25 | none in the repository |

Inside a lane the tasks run in order. Lanes meet only through the names below. A lane imports another lane's module only for what is listed under "Frozen exports". It may add exports to its own modules, but may not rename, remove or change the signature of a frozen one.

**Names used across tasks.**

- **Route components** (`routeTree.tsx` imports them by these names; a lane keeps the name and module):
  - `dashboard/HomePage.tsx::HomePage`
  - `inbox/InboxPage.tsx::InboxPage`
  - `inbox/InboxHistoryPage.tsx::InboxHistoryPage`
  - `documents/DocumentsPage.tsx::DocumentsPage`
  - `documents/DocumentDetailPage.tsx::DocumentDetailPage`
  - `imports/MappingEditor.tsx::MappingEditorPage`
  - `transactions/TransactionsPage.tsx::TransactionsPage`
  - `accounts/AccountsHomePage.tsx::AccountsHomePage`
  - `accounts/AccountHubPage.tsx::AccountHubPage`
  - `investments/InvestmentsPage.tsx::InvestmentsPage`
  - `subscriptions/SubscriptionsPage.tsx::SubscriptionsPage`
  - `subscriptions/SubscriptionDetail.tsx::SubscriptionDetail`
  - `budgets/BudgetsPage.tsx::BudgetsPage`
  - `budgets/BudgetDetailPage.tsx::BudgetDetailPage`
  - `goals/GoalsPage.tsx::GoalsPage`
  - `calendar/CalendarPage.tsx::CalendarPage`
  - `reports/ReportsPage.tsx::ReportsPage`
  - `networth/NetWorthPage.tsx::NetWorthPage`
  - `tax/TaxYearsPage.tsx::TaxYearsPage`
  - `tax/TaxYearPage.tsx::TaxYearPage`
  - `settings/SettingsLayout.tsx::SettingsLayout`
  - `settings/SettingsLayout.tsx::SettingsIndexPage`
  - The existing settings pages under their current names.
  - `categories/CategoriesSettingsPage.tsx::CategoriesSettingsPage`
  - `rules/RulesPage.tsx::RulesPage`
  - `rules/RuleEditorPage.tsx::NewRulePage`
  - `rules/RuleEditorPage.tsx::EditRulePage`
  - `imports/LearnedPage.tsx::LearnedPage`
  - `app/CommandPalette.tsx::CommandPalette`
- **Route parameters**, unchanged: `accountId`, `documentId`, `budgetId`, `id` (subscription), `year`, `ruleId`.
- **Search parameters** (contracts between lanes):
  - Transactions: `account`, `category` (id or `none`), `suggested=true`, `merchant`, `subscription`, `document`, `date_from`, `date_to`, `q`, `tx` (open drawer) and `always=<id>` (drawer with the Always box ticked).
  - Accounts: `group` (`cash`, `credit`, `investments`, `loans`, `property`).
  - Account hub: `tab` (`overview`, `transactions`, `documents`, `holdings`, `activity`, `performance`, `schedule`, `payments`, `rates`, `payoff`, `valuations`).
  - Investments: `tab` (`holdings`, `performance`, `activity`, `prices`), `account`.
  - Plan, Upcoming: `tab` (`calendar`, `recurring`).
  - Reports: `tab` and the existing `searchState` keys.
  - Inbox: `document`.
  - Settings, Categories: `tab` (`categories`, `payees`, `tags`, `tax`).
- **Query roots**: `queryRoots.inbox = ["inbox"]` (new). The live-update stream invalidates it on `document.*`, `review.*`, `subscription.*`, `schedule.*` and `transaction.*`.
- **`app/inboxSummary.ts`**: `inboxKeys.summary`, `getInboxSummary(): Promise<InboxSummary>`, `useInboxSummary()`. `InboxSummary = components["schemas"]["InboxSummary"]`.
- **Frozen exports** (lanes import them and must not change them):
  - `accounts/api.ts` (all current exports, `useCanWrite`)
  - `investments/api.ts`, `investments/format.ts`, `investments/performance.ts`, `investments/parts.tsx::AllocationBars`
  - `loans/api.ts`, `assets/api.ts`
  - `budgets/api.ts`, `budgets/report.ts`
  - `calendar/api.ts`
  - `goals/api.ts`, `goals/progress.ts`
  - `networth/api.ts`
  - `tax/api.ts`
  - `settings/api.ts`, `settings/saveFile.ts`
  - `categories/api.ts`, `categories/tree.ts`
  - `rules/api.ts`, `rules/JobProgress.tsx`
  - `transactions/api.ts`, `transactions/fixtures.ts`, `transactions/TransactionDialog.tsx`
  - `review/api.ts::listReviewCards`, `reviewKeys`
  - `subscriptions/api.ts` (all current exports)
  - everything in `features/planning/` and `features/auth/`
- **Section icons** (`components/icons.tsx`): `HomeIcon`, `InboxIcon` (exists), `ListIcon`, `CardIcon`, `TargetIcon`, `BarsIcon`, `GearIcon`, `SearchIcon`, `MoreIcon`, `ChevronRightIcon`, `UploadIcon`, `CheckIcon`.
- **Chart classes** (`app/styles/charts.css`): `chart-series-1` … `chart-series-6` set `fill` and `stroke` from `--color-series-N`; `chart-grid`, `chart-axis`. Chip colours: `chip--s1` … `chip--s6` from `lib/categoryColour.ts::categoryColourClass(category, tree)`.
- **Breakpoints**: `PHONE_MAX = 819`, `TABLE_CARDS_MAX = 719`, `ICON_RAIL_MAX = 1023` in `lib/useMediaQuery.ts`, mirrored as literals in CSS.

---

### Task 1: Tokens, fonts, theme colours, contrast and axe tooling (Start)

**Files:**
- Create: `frontend/src/app/styles/fonts.css`, `frontend/src/app/styles/charts.css`, `frontend/src/app/styles/contrast.ts`, `contrast.test.ts`, `frontend/src/test/axe.ts`, `frontend/src/test/axe.test.tsx`
- Modify: `frontend/src/app/styles/tokens.css`, `base.css`, `frontend/src/main.tsx` (imports `fonts.css` before `tokens.css`, and `charts.css`), `frontend/index.html` (the two `theme-color` metas), `frontend/public/manifest.webmanifest` (`theme_color`, `background_color`), `frontend/package.json`, `frontend/package-lock.json`

**Interfaces:**
- **`tokens.css`**:
  - Every current token name stays. The values come from spec section 2.1, in the light block and in both dark blocks.
  - New tokens: `--font-display`, `--font-size-3xl`, `--font-size-display`, `--rail-width`, `--rail-width-icon: 4.5rem`, `--bottom-bar-height: 3.75rem`, `--color-series-1` … `-6`, `--shadow-drawer`.
  - `--sidebar-width` and `--header-height` stay until Task 5 removes their last users.
- **`fonts.css`**: `@import "@fontsource-variable/figtree/index.css";` and `@import "@fontsource-variable/bricolage-grotesque/index.css";`. If those entry names differ in the installed version, use the package's `wght` entry. Vite rewrites the `url()`s to hashed files under `/static/`.
- **`base.css`**:
  - `body` uses `--font-sans` at `0.9375rem` (15px, the mock-up's body size); the root stays 16px.
  - `h1` to `h3` use `--font-display`, and `h1` is `--font-size-3xl`.
  - `.num` and `.money` get `font-variant-numeric: tabular-nums`.
  - `:focus-visible` is a 2px outline in `--color-focus` with a 2px offset.
- **`charts.css`**: the names under "Chart classes".
- **`contrast.ts`**:
  - `parseThemes(css: string): { light: Map<string, string>; dark: Map<string, string>; darkForced: Map<string, string> }`. It reads the `:root`, the media `:root:not([data-theme="light"])` and the `:root[data-theme="dark"]` blocks; `var(--x)` resolves inside a theme.
  - `ratio(foreground: string, background: string): number`, using WCAG 2.2 relative luminance on `#rrggbb`.
  - `PAIRS: readonly { fg: string; bg: string; min: number }[]`, text at 4.5:
    - `--color-text` on `bg`, `surface` and `surface-sunken`.
    - `--color-text-muted` on the same three.
    - `--color-accent` on `surface` and on `accent-soft`.
    - `--color-accent-contrast` on `accent`.
    - `--color-warning`, `-danger`, `-info` on their `-soft` and on `surface`.
    - `--color-text` on `accent-soft`.

    UI at 3:
    - `--color-border-strong` on `surface` and `bg`.
    - `--color-focus` on `surface` and `bg`.
    - each `--color-series-N` on `surface`.
- **`test/axe.ts`**: `export async function expectNoAxeViolations(container: Element, options?: { rules?: Record<string, { enabled: boolean }> }): Promise<void>`. It runs `axe.run` with `color-contrast` disabled, plus `region` disabled when the container is not the whole document. It fails with each violation's id, help and first three targets.
- **`package.json`**: `dependencies` gains the two fontsource packages at `5.3.0`; `devDependencies` gains `axe-core` at `4.14.1`.

- [ ] **Step 1: Failing tests.**
  - `contrast.test.ts`:
    - `each pair meets its minimum in the light theme`.
    - `… in the dark theme (media block)`.
    - `… in the dark theme (forced block)`.
    - `the two dark blocks declare the same values`.
    - `every token named in a pair exists in all three blocks`.
    - The pair tests read `tokens.css` from disk with `node:fs`, as `pwa.test.ts` does with `index.html`.
  - `axe.test.tsx`:
    - `passes a labelled button`.
    - `fails an unlabelled icon button and names the rule` (`button-name`).
  - `src/pwa/pwa.test.ts`: run it; the colour format assertions must still pass.
- [ ] **Step 2:** Run `npm test -- src/app/styles src/test src/pwa`. Expected: missing series tokens and pairs below their minimum (the old palette), and `axe-core` not installed.
- [ ] **Step 3: Implement.**
  - `npm install --save-exact @fontsource-variable/figtree@5.3.0 @fontsource-variable/bricolage-grotesque@5.3.0` and `npm install --save-exact --save-dev axe-core@4.14.1`.
  - Confirm each new `resolved` URL in `package-lock.json` starts with `https://registry.npmjs.org/`, which CI checks.
  - Write the token values and the new files.
- [ ] **Step 4:** Run the same tests, then `npm run lint`, `npm run typecheck` and `npm run build`. In `dist/static/` confirm the `.woff2` files exist and that `dist/index.html` has no inline `<style>`. Expected: pass.
- [ ] **Step 5:** Commit `feat(ui): design tokens for the redesign, self-hosted fonts, contrast and axe checks`.

### Task 2: `GET /inbox/summary` (Start)

**Files:**
- Create: `backend/app/domain/inbox/__init__.py`, `schemas.py`, `service.py`, `backend/app/api/routers/inbox.py`, `backend/tests/integration/test_inbox_api.py`
- Modify: `backend/tests/security/authz_policy.py` (one line), `backend/openapi.json`, `frontend/src/lib/api/schema.d.ts`
- Test: `backend/tests/security/test_authz_matrix.py`, `test_input_models.py`, `tests/security/mcp/test_mcp_server.py`, `tests/security/ai/test_ai_documents.py` (run, not edited)

**Interfaces:**
- **`schemas.py`**: `class InboxSummary(BaseModel)` with the fields `documents: int`, `loose: int`, `subscriptions: int`, `suggestions: int`, `uncategorised: int` and `cards: int`, all `>= 0`.
- **`service.py`**: `summary(db: Session, principal: Principal) -> InboxSummary`. Each count reuses an existing function:
  - **`documents`**: the count of distinct `ReviewItem.document_id`, where `household_id` is the caller's, `review.service.visible_filter(principal)` holds, `state == OPEN` and `document_id IS NOT NULL`.
  - **`loose`**: the same with `document_id IS NULL`.
  - **`suggestions` and `uncategorised`**: from `categorisation.service.summary(db, principal)`.
  - **`subscriptions`**: `len({series with status "suggested"} ∪ {schedule_id of open events})`. The suggested series come from `recurring.subscriptions.list_subscriptions(db, principal, kind=None, status=("suggested",), account_ids=None)`. The open events come from `list_events(db, principal, state="open", cursor=…, limit=200)`, paged.
  - **`cards`**: `documents + loose + subscriptions + (1 if suggestions else 0)`. "Make it a rule" offers are not counted, and the learned list is not computed here.
- **Router**: `GET /inbox/summary`, `response_model=InboxSummary`, `responses=PROBLEMS`, `summary="What waits on a person"`, with dependency `Reader`.
- **Policy**: `f"GET {API}/inbox/summary": scoped("review:read", role=V)`.

- [ ] **Step 1: Failing tests** in `test_inbox_api.py`:
  - `test_empty_household_is_all_zero`.
  - `test_counts_documents_and_loose_items` (two open items on one document and one item on another: `documents == 2`; one item with no document: `loose == 1`).
  - `test_counts_a_series_once` (a suggested series that also has an open event counts 1).
  - `test_suggestions_and_uncategorised_match_the_categorisation_summary`.
  - `test_cards_adds_one_for_suggestions_and_nothing_for_rule_offers` (a learned entry with three unanimous decisions does not change `cards`).
  - `test_a_viewer_sees_only_granted_accounts` (a document, a suggestion and a series on an account without a grant are not counted).
  - `test_unauthenticated_is_401`.
- [ ] **Step 2:** Run `uv run pytest -q tests/integration/test_inbox_api.py tests/security/test_authz_matrix.py`. Expected: 404 and a missing policy line.
- [ ] **Step 3: Implement**, then `make openapi` (it regenerates `openapi.json` and `schema.d.ts`).
- [ ] **Step 4:** Run the files above together with `tests/security/test_input_models.py`, `tests/security/mcp/test_mcp_server.py` and `tests/security/ai/test_ai_documents.py`. The tool count must stay 19 and the route count 19. Also run `uv run perfin openapi | diff -u openapi.json -` and, in `frontend/`, `npm run typecheck`. Expected: pass, no drift.
- [ ] **Step 5:** Commit `feat(api): one count of what waits on a person, for the Inbox badge`.

### Task 3: Shared primitives (Start)

**Files:**
- Create in `frontend/src/components/`: `Drawer.tsx`, `Menu.tsx`, `SectionNav.tsx`, `PageHeader.tsx`, `Breadcrumbs.tsx`, `Stat.tsx`, `Chip.tsx`, `ProgressBar.tsx`, `StackBar.tsx`, `Sparkline.tsx`, `QuietNote.tsx`. Tests: `Drawer.test.tsx`, `Menu.test.tsx`, `SectionNav.test.tsx`, `PageHeader.test.tsx`, `Stat.test.tsx`, `Chip.test.tsx`, `graphics.test.tsx` (ProgressBar, StackBar, Sparkline, QuietNote).
- Create in `frontend/src/lib/`: `urlState.ts`, `urlState.test.ts`, `useMediaQuery.ts`, `useMediaQuery.test.ts`, `categoryColour.ts`, `categoryColour.test.ts`.
- Create: `frontend/src/features/categories/NewCategoryDialog.tsx`, `NewCategoryDialog.test.tsx`.
- Modify: `components/index.ts`, `components.css`, `components.test.tsx`, `icons.tsx`, `Table.tsx`, `Tabs.tsx`, `CategoryPicker.tsx`, `Card.tsx` (class only)

**Interfaces:**
- **`Drawer`**: `{ open: boolean; onClose(): void; title: string; description?: string; children; footer?: ReactNode; initialFocus?: RefObject<HTMLElement> }`.
  - It renders a `<dialog>` used with `showModal()`, as `Dialog` does, with the class `drawer`.
  - Escape closes it, and focus returns to the opener.
  - Under 820 px the CSS makes it a full-height sheet.
  - The slide animation runs only under `prefers-reduced-motion: no-preference`.
- **`Menu`**: `{ label: string; items: { id: string; label: string; onSelect(): void; disabled?: boolean; danger?: boolean }[]; trigger?: ReactNode }`.
  - It follows the WAI-ARIA menu button pattern (Arrow keys, Home, End, Escape, typeahead), moved from `transactions/RowActions.tsx`.
  - It positions itself with the CSSOM, which is the one allowed CSSOM write.
- **`SectionNav`**: `{ label: string; items: { to: string; label: string; search?: Record<string, string>; keepSearch?: readonly string[]; count?: number }[] }`.
  - It is a `<nav aria-label={label}>` of `Link`s. The active item has `aria-current="page"`, matching the pathname and the `tab` search value when an item sets one.
  - A `count` is shown in a badge and added to the accessible name ("Needs you, 3").
- **`PageHeader`**: `{ title: string; description?: string; actions?: ReactNode; breadcrumbs?: { to?: string; label: string }[] }`. It renders `h1`, or `h2` when `useContext(SectionHeadingContext)` is true. `SectionHeadingContext` is exported from `PageHeader.tsx`.
- **`Stat`** and **`StatGroup`**: `Stat { label: string; value: ReactNode; change?: { text: string; tone: "up" | "down" | "neutral" }; size?: "md" | "display" }`. `StatGroup` is a `<dl>`.
- **`Chip`**: `{ tone?: "neutral" | "ok" | "warn" | "bad" | "info"; colourClass?: string; pressed?: boolean; onClick?(): void; children }`.
  - With `onClick` it is a `<button>` with `aria-pressed` when `pressed` is set; otherwise a `<span>`.
  - Its minimum height is 24px.
- **`ProgressBar`**: `{ value: number; max: number; label: string; over?: boolean }`. It is an `<svg viewBox="0 0 100 8" role="progressbar" aria-valuenow aria-valuemin="0" aria-valuemax aria-label>` with a track rect and a value `rect` whose `width` attribute is `min(100, value / max * 100)`.
- **`StackBar`**: `{ label: string; parts: { label: string; value: number; colourClass: string }[] }`. It renders an `<svg role="img" aria-label>` of rects, followed by a legend `<ul>` with the amounts.
- **`Sparkline`**: `{ points: number[]; label: string }`. It is an `<svg role="img" aria-label viewBox="0 0 300 120" preserveAspectRatio="none">` with an area path and a line path; the path `d` is computed.
- **`QuietNote`**: `{ children }`. It renders a span with a dot and muted text.
- **`Table`** gains `rowLink?: (row) => { label: string; onOpen(): void } | null`. The first cell then holds a `<button className="table__row-link">` with that label, and the row's click and Enter forward to it.
- **`Tabs`**: unchanged API; `selectedId` and `onSelect` already exist.
- **`lib/urlState.ts`**:
  - `useSearchParam(name: string, fallback: string): [string, (value: string) => void]`
  - `useTabParam<T extends string>(ids: readonly T[], fallback: T, name = "tab"): [T, (id: T) => void]`

  Both write with `navigate({ search: (prev) => …, replace: true })`, leave defaults out, and ignore unknown values.
- **`lib/useMediaQuery.ts`**: `useMediaQuery(query: string): boolean` (SSR-safe, with a listener) and the three breakpoint constants.
- **`lib/categoryColour.ts`**: `categoryColourClass(categoryId: string | null, tree: readonly { id: string; parent_id: string | null; system_key: string | null }[]): string`.
  - It resolves to the top-level category.
  - The default groups map to fixed classes (`food` s1, `transport` s2, `leisure` s3, `shopping` s4, `housing` and `utilities` s6, …; the table is in the file).
  - Any other category gets s1 to s6 from a stable FNV-1a hash of its id.
  - `null` gives `chip--none`.
- **`CategoryPicker`** gains `onCreate?: () => void`. When it is set, the last option is "New category…" (value `__new__`), and choosing it calls `onCreate` instead of `onChange`.
- **`NewCategoryDialog`**: `{ open: boolean; onClose(): void; onCreated(category: Category): void; parentId?: string }`. It has a name field, a parent select (top-level categories) and `POST /categories`. It reports the API's validation problem in the field, and invalidates `queryRoots.taxonomy`.
- **Icons**: the list under "Section icons".
- **`components.css`**: the mock-up's panel, button, chip, tab, table, drawer and toast looks, mapped onto the existing classes and the new ones (`drawer`, `section-nav`, `page-header`, `breadcrumbs`, `stat`, `chip`, `progress`, `stack`, `sparkline`, `quiet`). Every colour comes from a token.

- [ ] **Step 1: Failing tests.**
  - **Drawer**: `opens as a labelled dialog and traps focus`; `Escape closes and focus returns to the opener`.
  - **Menu**: `arrow keys move, Enter selects, Escape closes and returns focus`; `typeahead`.
  - **SectionNav**: `marks the current item by path`; `by tab search value`; `keeps the named search parameters`; `a count is in the name`.
  - **PageHeader**: `h1 outside a section, h2 inside`; `breadcrumbs are a nav with the last item current`.
  - **Stat**: a `<dl>` with label and value; the change tone is in the text and not only in colour ("up 8%").
  - **Chip**: `a pressable chip is a button with aria-pressed`.
  - **graphics**: ProgressBar reports `aria-valuenow`, `aria-valuemax` and the clamped width attribute; StackBar has a legend with every part; Sparkline has a path and its label.
  - **`urlState.test.ts`**: `useTabParam` reads the value, ignores an unknown one and writes with replace, leaving the default out.
  - **`categoryColour.test.ts`**: a leaf resolves to its group's class; the hash is stable and in range; null gives `chip--none`.
  - **`NewCategoryDialog.test.tsx`**: creates with name and parent; shows the server's 422 under the field.
  - **`components.test.tsx`**: CategoryPicker's `onCreate` entry; Table's `rowLink` button and Enter.
  - **Every new test file**: one `has no axe violations` case.
- [ ] **Step 2:** Run `npm test -- src/components src/lib src/features/categories/NewCategoryDialog.test.tsx`. Expected: missing modules.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the same, then `npm run lint` and `npm run typecheck`. Expected: pass.
- [ ] **Step 5:** Commit `feat(ui): drawer, menu, section nav, page header, stats, chips and SVG bars`.

### Task 4: Shared page vocabulary and chart palette (Start)

**Files:**
- Modify: `frontend/src/features/planning/PlanningPage.tsx` (renders `PageHeader`), `planning.css`, `frontend/src/features/accounts/ledger.css`, `frontend/src/features/networth/wealth.css`, `networth/SeriesChart.tsx` (series and grid through `charts.css` classes, `aria-label` and a table toggle kept), `frontend/src/features/auth/auth.css` (the settings card list and the narrow-header rules become the new panel look; the header rules go when Task 5 removes the header)
- Create: `frontend/src/features/networth/SeriesChart.test.tsx`

**Interfaces:**
- `PlanningPage { title, actions?, children, description?, breadcrumbs? }` renders through `PageHeader`. It therefore gets `h2` inside a section layout. Its current props keep working.
- `ledger.css`, `planning.css` and `wealth.css`: same class names, restyled to the mock-up's panel and table look. No class is renamed, so every importing feature restyles without an edit.
- `SeriesChart`: Recharts `Line` and `Area` get `className="chart-series-N"` and no `stroke` or `fill` props.

- [ ] **Step 1: Failing tests:**
  - `SeriesChart.test.tsx`: `draws each series with its class and no colour attribute` (no `stroke="#`); `has a label naming the series`.
  - `src/features/planning`: run the existing tests that render `PlanningPage` pages (`npm test -- src/features/goals src/features/budgets`). Expected: pass.
- [ ] **Step 2:** Run `npm test -- src/features/networth`. Expected: the colour attributes are found.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the whole frontend suite (`npm test`). Expected: pass. A failure caused by a heading level is fixed in that test file, which is named in the commit message.
- [ ] **Step 5:** Commit `feat(ui): the shared page and chart styles in the new look`.

### Task 5: Shell, route map, route tree, redirects, lazy pages, badge, bundle budget (Start)

**Files:**
- **Create in `frontend/src/app/`**: `routeMap.ts`, `routeMap.test.ts`, `routeTree.tsx`, `redirects.tsx`, `redirects.test.tsx`, `SectionLayout.tsx`, `SectionLayout.test.tsx`, `inboxSummary.ts`, `InboxBadge.tsx`, `InboxBadge.test.tsx`, `CommandPalette.tsx` (a placeholder that renders `null`; lane K owns it after this task).
- **Create the bridges** (Decision 3): `frontend/src/features/dashboard/HomePage.tsx`, `features/inbox/InboxPage.tsx`, `features/inbox/InboxHistoryPage.tsx`, `features/accounts/AccountsHomePage.tsx`, `features/accounts/AccountHubPage.tsx`, `features/categories/CategoriesSettingsPage.tsx`.
- **Create** `frontend/scripts/check-bundle.mjs` and `frontend/scripts/check-bundle.test.mjs`.
- **Modify**:
  - `app/nav.ts`: becomes a thin re-export of the rail items from `routeMap.ts`.
  - `app/router.tsx`, `app/RootLayout.tsx`, `app/styles/layout.css` (rewritten), `app/pages.tsx` (`PlaceholderPage` and `LoginPage` removed), `app/shell.test.tsx` (rewritten).
  - `app/styles/tokens.css`: drop `--sidebar-width` and `--header-height`.
  - `features/auth/AccountMenu.tsx` and `AccountMenu.test.tsx`: it sits in the rail foot and the phone top bar; it gains a "Settings" item.
  - `lib/queryKeys.ts` (`inbox`), `lib/sse.ts`, `lib/sse.test.ts`.
  - `features/loans/LoanDetailPage.tsx` and `features/assets/AssetDetailPage.tsx` (the optional id props).
  - `Makefile` (`frontend-check` runs `npm run check:bundle` after the build), `.github/workflows/ci.yml` (the same step after `npm run build`), `frontend/package.json` (script `"check:bundle": "node scripts/check-bundle.mjs"`).
- **Delete**: `frontend/src/app/shellRoutes.tsx` and every `frontend/src/features/*/routes.tsx` except `auth/routes.tsx` and `setup/routes.tsx`. That is accounts, assets, budgets, calendar, dashboard, documents, goals, imports, investments, loans, networth, reports, review, rules, settings, subscriptions, tax and transactions.
- **Any test file**: path, heading-level and `findBy` changes only, each file named in the commit message.

**Interfaces:**
- **`routeMap.ts`**:
  - `SECTIONS: readonly Section[]` with `Section { id: "home" | "inbox" | "transactions" | "accounts" | "plan" | "reports"; label; to; icon; description; tabs: SectionTab[] }`.
  - `SETTINGS_GROUPS: readonly { label: string; pages: { to: string; label: string; instanceAdmin?: true }[] }[]` (spec section 1.3).
  - `REDIRECTS: readonly Redirect[]` with `Redirect { from: string; to: string; search?: Record<string, string>; data?: "loan" | "asset" }`. There are 24 rows, one per "redirect" row of the spec's table (the `/plan` index redirect is part of the tree, not of this list).
  - `OLD_ROUTES: readonly string[]`: every path registered at the stage 2 head, generated in Step 1 (below) and pasted as a literal, with the command in a comment above it.
  - `NEW_ROUTES: readonly string[]`: every path the tree registers that is not a redirect.
  - Section tab bars:
    - Inbox: "Needs you" `/inbox` (with `count` from the summary), "Documents" `/inbox/documents`, "History" `/inbox/history`.
    - Accounts: "All accounts" `/accounts`, "Investments" `/accounts/investments`.
    - Plan: Subscriptions, Budgets, Goals, Upcoming.
    - Reports: Spending `/reports`, Income `/reports` with `search { tab: "income" }`, "Cash flow" with `cashflow`, Trends with `trends`, "Net worth" `/reports/net-worth`, Tax `/reports/tax`. The four `?tab` items keep the Decision 5 keys.
- **`routeTree.tsx`**: `buildRouteTree(root)`.
  - It creates the layouts: `/inbox`, `/accounts`, `/plan` and `/reports` with `SectionLayout` (taking the section id), and `/settings` with `SettingsLayout`.
  - Each page in the spec's table is a child of its layout. Detail pages have `staticData: { detail: true }`. `/plan` has an index child that redirects to `/plan/subscriptions`.
  - Every component is loaded with `lazyRouteComponent(() => import("…"), "Name")`, except `HomePage` and `TransactionsPage`, which are imported directly.
  - It appends the auth and setup routes and `redirectRoutes(root)`.
- **`redirects.tsx`**: `redirectRoutes(root): AnyRoute[]`.
  - It makes one route per `REDIRECTS` row. Each throws `redirect({ to, params, search: (prev) => ({ ...prev, ...row.search }), hash: true, replace: true })` from `beforeLoad`.
  - `/reports`'s `tab` is not a redirect. `/review?document=` keeps `document` through the search spread.
  - **Data rows**: `/loans/$loanId` and `/assets/$assetId` render `AccountRedirect`. It reads the object (`api.GET`), then navigates `replace` to `/accounts/$accountId` with `tab` `schedule` or `valuations`. On a 404 it says "This loan no longer exists." (or asset) with a link to Accounts. While loading it shows a `Spinner`.
- **`SectionLayout`**: `{ section: Section["id"] }`.
  - When the deepest match has `staticData.detail`, it renders only `<Outlet/>`.
  - Otherwise it renders `<header class="section-header">` with `h1` (the label), the description, and `SectionNav` (`aria-label` = the label). Then it renders `SectionHeadingContext.Provider value={true}` with `<Outlet/>`.
- **`RootLayout`**:
  - **Rail** (`nav aria-label="Sections"`): brand, the six sections with icons, the Inbox item with `InboxBadge`, a spacer, Settings, `AccountMenu` and `ThemeToggle`.
  - **Skip link**, AI banner and `main` as today. `CommandPalette` is mounted once.
  - **Under 820 px**: a top bar (brand, `AccountMenu`, `ThemeToggle`) and a bottom bar holding the same six section links. Both are made with CSS, so the links exist once and their names do not change.
  - **Between 820 and 1023 px**: the icon rail. Labels are visually hidden.
  - `main` has `scroll-padding-bottom: var(--bottom-bar-height)` under 820 px.
- **`InboxBadge`**: it uses `useInboxSummary()` and renders `<span class="badge">{cards}</span>` with the visually hidden text "things need you". It renders nothing at zero, while loading and on error.
- **`sse.ts`** adds `queryRoots.inbox` to `document.*`, `review.*`, `subscription.*`, `schedule.*` and `transaction.*` (stage 2 already has `transaction.categorised`).
- **`check-bundle.mjs`**:
  - `measure(distDir) -> { initialJs, largestLazyJs, totalJs, totalCss, fonts }` in gzip bytes (`zlib.gzipSync`, level 9), with `fonts` counting only the Latin `woff2` files (names containing `latin-wght` and not `ext`).
  - `BUDGET` as in spec section 10, and `BASELINE_TOTAL_JS`, the gzip total measured on the stage 2 head before this task. Write that figure and the date in the header comment.
  - It exits 1 with a table of measured against budget.
  - `check-bundle.test.mjs` runs with `node --test` on a fixture `dist` built in a temp dir.

- [ ] **Step 1: Baseline.** On the stage 2 head, before any change of this task:
  - Run `npm run build`, record the gzip sizes of every JS and CSS file, and keep the figures for the commit message and the script header.
  - Generate the old routes: `git grep -hoE 'path: "[^"]*"|page\((LOGIN|MFA|ENROLL|LINK_OIDC|FORGOT|RESET)_PATH|child\("[^"]*"' HEAD -- 'frontend/src/features/*/routes.tsx'`, plus the `NAV_ITEMS` paths of `app/nav.ts`, the auth constants of `features/auth/guard.ts` and `SETUP_PATH`. Resolve settings children against `/settings` and the subscription child against `/subscriptions`, sort, and paste the result into `OLD_ROUTES`.
- [ ] **Step 2: Failing tests.**
  - **`routeMap.test.ts`**:
    - `every old route has a home`: each entry of `OLD_ROUTES` is a new route or the `from` of exactly one redirect, and the list has no duplicates. The list itself is not hand-typed: it is the output of Step 1's command.
    - `every old route is either a new route or the from of exactly one redirect`.
    - `no redirect target is itself redirected`.
    - `every section tab points at a new route`.
  - **`redirects.test.tsx`**: one `it.each` over `REDIRECTS`, each case rendering at the old path through `renderApp` with a search and a hash. It asserts the new pathname, the merged search, the hash, and the target page's heading. Also:
    - `/loans/l-1 goes to the loan's account with the schedule tab`.
    - `/assets/x-1 goes to the asset's account with the valuations tab`.
    - `a missing loan says so and links to Accounts`.
    - `/plan opens subscriptions`.
    - `/review?document=d-1 keeps the document`.
  - **`shell.test.tsx`** (rewritten):
    - `the rail lists the six sections in order, then Settings`.
    - `the current section is aria-current`.
    - `the Inbox badge shows the card count and hides at zero and on error`.
    - `bare pages have no rail`.
    - `the skip link targets main`.
    - `the account menu has Settings and Sign out`.
    - `has no axe violations`.
  - **`SectionLayout.test.tsx`**:
    - `renders the section h1 and its tab bar`.
    - `a detail page gets neither`.
    - `a page header inside renders h2`.
  - **`InboxBadge.test.tsx`**: covered by `shell.test.tsx`; plus `the name reads "3 things need you"`.
  - **`sse.test.ts`**: `review and subscription events invalidate the inbox count`.
  - **`check-bundle.test.mjs`**: `sums the entry and its preloads`; `fails over budget with the figures`.
- [ ] **Step 3:** Run `npm test -- src/app src/lib/sse.test.ts` and `node --test scripts/check-bundle.test.mjs`. Expected: missing modules, the old labels, no redirects.
- [ ] **Step 4: Implement.** Write the bridges as thin components that render today's pages; no new behaviour. Then run the **whole** frontend suite. For each red test outside `src/app`:
  - **The test renders an old path whose page is unchanged**: it should pass through the redirect. If it fails, fix the redirect, not the test.
  - **It fails on `getBy` right after `renderApp` because the page is now lazy**: change that query to `findBy`.
  - **It fails on a heading level now taken by the section `h1`**: drop the level or use the new one.

  Name every test file you change in the commit message.
- [ ] **Step 5:** Run `npm run lint`, `npm run typecheck`, `npm test`, `npm run build` and `npm run check:bundle`, then `make frontend-check`.
  - Expected: pass.
  - The initial JS is under budget now that Recharts and the review tools are lazy. A figure over budget is reported to the coordinator with the measurements; the task does not raise a budget.
  - If the fonts are over 120 kB, apply Decision 10 here (`fonts.css`, `tokens.css`).
  - Record the measured figures in the commit message.
- [ ] **Step 6:** Commit `feat(ui): six sections and Settings, one route map, redirects from every old address, lazy pages`.

### Task 6: Home (lane H)

**Files:**
- Create: `frontend/src/features/dashboard/home.ts`, `home.test.ts`, `panels.tsx`, `HomePage.test.tsx`
- Modify: `features/dashboard/HomePage.tsx` (the bridge becomes the page), `api.ts` (adds `getSpendingComparison(from, to, previousFrom, previousTo)`, `getNetWorthSeries()`, `getComingUp()`), `dashboard.css`, `helpers.ts`, `GettingStarted.tsx`, `GettingStarted.test.tsx`
- Delete: `features/dashboard/DashboardPage.tsx`, `DashboardPage.test.tsx`, `widgets.tsx`, `layout.ts`, `layout.test.ts`, `useDashboardLayout.ts`, `customize.test.tsx`

**Interfaces:**
- **`home.ts`** (pure):
  - `greeting(now: Date): "Good morning" | "Good afternoon" | "Good evening"` (5:00 to 11:59, 12:00 to 17:59, otherwise).
  - `comparison(current: string, previous: string): { text: string; tone: "up" | "down" | "neutral" }`. It produces "8% less than this point in October" and "12% more…", and when previous is 0, "Nothing spent by this point in October". `current` and `previous` are decimal strings.
  - `topCategories(rows, n = 5)`: the five largest top-level categories plus "Other".
  - `comingUp(subscriptions, recurring, today, days = 30, max = 5)`: merged, ordered by date, no duplicates by schedule id. Each item has a note from `price_change` ("went up $2.00 last month") or the amount range ("usually $105 to $125"; amount shown as "~$118").
- **`HomePage`**: spec section 3. The panels sit in `panels.tsx`: `NetWorthPanel`, `NeedsYouPanel`, `SpendingPanel`, `ComingUpPanel`, `LatestPanel`.
  - Each panel has its own query, spinner and one-line error with Retry.
  - `NeedsYouPanel` uses `useInboxSummary()` and `listReviewCards()` (first page; the first three cards in the Inbox order). It sorts them with the spec's order, using a local copy of the action groups; lane I owns `inbox/order.ts`, and Task 21 replaces the copy with an import. The panel adds the uncategorised line.
  - `LatestPanel` rows link to `/transactions?tx=<id>`.
  - `GettingStarted` replaces the panels when there are no accounts.
  - Links point to the new paths.
  - The amounts use `MoneyText`. The sparkline uses `Sparkline`, the spending bar `StackBar`, and the chips `categoryColourClass`.

- [ ] **Step 1: Failing tests.**
  - **`home.test.ts`**: greeting boundaries; comparison texts and the zero case; top categories with "Other"; `comingUp` merges, orders, caps at five, and writes the two kinds of note.
  - **`HomePage.test.tsx`** (at `/`):
    - `shows net worth with its change and a sparkline label`.
    - `needs you lists the first three cards in Inbox order and the uncategorised line`.
    - `needs you says nothing is waiting`.
    - `spending compares with the same day last month`.
    - `coming up shows a price change note`.
    - `latest transactions open the drawer link`.
    - `a household with no accounts sees Start here`.
    - `one failing panel does not hide the others`.
    - `has no axe violations`.
- [ ] **Step 2:** Run `npm test -- src/features/dashboard`. Expected: missing modules.
- [ ] **Step 3: Implement**, and delete the files listed. Grep that nothing imports `widgets`, `layout` or `useDashboardLayout`: `grep -rn "dashboard/widgets\|dashboard/layout\|useDashboardLayout" frontend/src` should find nothing.
- [ ] **Step 4:** Run the tests, `npm run lint` and `npm run typecheck`. Expected: pass.
- [ ] **Step 5:** Commit `feat(home): one fixed page: net worth, what needs you, this month, coming up, latest`.

### Task 7: Inbox, Needs you: the cards and their order (lane I)

**Files:**
- Create: `frontend/src/features/inbox/order.ts`, `order.test.ts`, `cards.tsx` (`SubscriptionCard`, `SuggestionsCard`, `RuleOfferCard`), `inbox.css`, `InboxPage.test.tsx`
- Modify: `features/inbox/InboxPage.tsx` (the bridge becomes the page), `features/review/ReviewPage.tsx` (exports `ReviewCardList`, the card list without its page header and filter, for the Inbox), `review/DocumentCard.tsx` (styles and `PageHeader`; the behaviour is unchanged), `review/review.css`, `review/ReviewPage.test.tsx` (paths become `/inbox`; content assertions unchanged)

**Interfaces:**
- **`order.ts`**:
  - `ACTION_GROUP: ReadonlyMap<Action, 1 | 2 | 3>` (spec section 4.2).
  - `orderCards(cards: ReviewCard[]): ReviewCard[]`, by group, then `document.created_at` ascending. Unknown actions go in group 3.
  - `subscriptionCards(series, events)`: one entry per series, holding its open events. Series with an open event come first (by their newest event, newest first), then suggested series with none (by `occurrences`, descending).
- **`InboxPage`**, in order:
  1. The upload zone. It moves in Task 8; in this task the bridge's `UploadPage` stays.
  2. The document cards (`ReviewCardList` sorted by `orderCards`), then the loose items.
  3. The subscription cards, one per series (`GET /subscriptions?status=suggested`, `GET /subscriptions/events?state=open`). A card lists each open change in words; it has Confirm and "Not a subscription" for a suggested series and "Got it" for its changes (one `events/seen` call with the card's event ids), through `subscriptions/api.ts` (frozen). The buttons are hidden for a viewer.
  4. `SuggestionsCard` (`GET /categorisation/summary` and `listTransactions({ suggested: true, limit: 200 })`).
     - It groups by `merchant_key` and shows the top five, with "Accept N". That is the bulk `accept_suggestions` through `transactions/api.ts` with the loaded ids.
     - It links "Review all in Transactions" to `/transactions?suggested=true`.
     - Under it: "N have no category" links to `/transactions?category=none`.
     - Members and above see Accept.
  5. `RuleOfferCard`, up to three, from `GET /categorisation/learned`. "Always…" goes to `/transactions?always=<example_transaction_id>`; "Not now" hides it in the session's state.
  6. Empty: "All clear…" (spec section 4.3).
- **`?document=<id>`** shows only that card, with "Show everything".
- **The settled card stays** after its answer, as today (`SettledCard`).
- **Counting**: the number of cards on the page equals `InboxSummary.cards` when everything fits on the first page. A test pins this.

- [ ] **Step 1: Failing tests.**
  - **`order.test.ts`**: the three groups in order, oldest first inside a group, unknown actions last; subscription items in order.
  - **`InboxPage.test.tsx`** (at `/inbox`):
    - `document cards come first in the spec's order`.
    - `a subscription price change can be acknowledged` (the `events/seen` body).
    - `a suggested series can be confirmed or dismissed` (the decision bodies).
    - `suggestions are grouped by merchant and accepted per merchant` (the bulk body with that merchant's ids only).
    - `accepting never sends a row without a suggestion`.
    - `a rule offer links to the drawer`.
    - `a viewer sees the cards without buttons`.
    - `the number of cards that need an answer equals the summary's cards` (rule offers excluded; a suggested series with an open change is one card).
    - `?document= shows one card`.
    - `the empty state`.
    - `has no axe violations`.
  - **`ReviewPage.test.tsx`**: change `renderApp("/review…")` to `renderApp("/inbox…")` and run it; the stage 1 card behaviour must still pass.
- [ ] **Step 2:** Run `npm test -- src/features/inbox src/features/review`. Expected: missing modules; order not applied.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the tests, `npm run lint` and `npm run typecheck`. Expected: pass.
- [ ] **Step 5:** Commit `feat(inbox): everything that waits on a person, one card each, in the order that matters`.

### Task 8: Inbox: upload, imported recently, history (lane I)

**Files:**
- Create: `frontend/src/features/inbox/UploadZone.tsx`, `RecentImports.tsx`, `InboxHistoryPage.test.tsx`
- Modify: `features/inbox/InboxPage.tsx`, `InboxHistoryPage.tsx` (the bridge becomes the page), `features/imports/UploadPage.tsx` (its upload logic moves into `UploadZone`; the page is deleted when nothing imports it), `imports/api.ts`, `imports/UploadPage.test.tsx` (moves to `inbox/UploadZone.test.tsx`), `features/review/ReviewPage.tsx` (the history list is exported as `ReviewHistory`)
- Delete: `features/imports/UploadPage.tsx` and `UploadPage.test.tsx` once their content has moved

**Interfaces:**
- **`UploadZone`**: today's XHR upload with progress, a drop zone (a `<label>` over an `<input type="file" multiple>`, which keyboard and screen readers can reach), and per-file rows.
  - A finished row links to `/inbox/documents/<id>` (audit finding 1).
  - A file that needs a person says "Waiting on you below", with a link that scrolls to its card.
- **`RecentImports`**: `GET /documents?limit=25`, kept on the client when `status` is `processed` or `failed` and `updated_at` is within the last 14 days, at most 10.
  - Each row has the file name (a link), the account (a link, from `account_id` and the accounts list), the period, and `QuietNote` "Not checked against a balance" when `unchecked`.
  - Failed files say "Could not be read".
- **`InboxHistoryPage`**: today's Review filter for Resolved, Dismissed and All (`useSearchParam("show", "resolved")`), as a list.

- [ ] **Step 1: Failing tests.**
  - **`UploadZone.test.tsx`**: the upload tests of today at the new place; `a finished file links to its document`.
  - **`InboxPage.test.tsx`**: `recently imported names the account and marks unchecked reads`; `a failed file says so`.
  - **`InboxHistoryPage.test.tsx`**: `lists resolved items by default and switches to dismissed`; `has no axe violations`.
- [ ] **Step 2:** Run `npm test -- src/features/inbox src/features/imports`. Expected: no links, no history page.
- [ ] **Step 3: Implement.** `grep -rn "UploadPage" frontend/src` should then find only the history of the git log.
- [ ] **Step 4:** Run the tests, `npm run lint` and `npm run typecheck`. Expected: pass.
- [ ] **Step 5:** Commit `feat(inbox): upload on the Inbox, where each file went, and the answered items`.

### Task 9: Inbox, Documents: library, detail, mapping (lane I)

**Files:**
- Modify: `frontend/src/features/documents/DocumentsPage.tsx`, `DocumentDetailPage.tsx`, `PageViewer.tsx` (word and region boxes become an SVG overlay: `<svg viewBox="0 0 W H">` with a `<rect x y width height>` per box; the four `style` sites go), `documents.css`, `DocumentsPage.test.tsx`, `AttachDialog.tsx`, `AttachDialog.test.tsx`, `StatementSummaryCard.tsx`, `features/imports/MappingEditor.tsx` (after Save it returns to `/inbox/documents/<id>`), `mapping.test.tsx`, `DocumentAccountsDialog.tsx`, `DocumentAccountsDialog.test.tsx`, `features/review/TeachDialog.tsx` (its outcome links to `/settings/learned`), `TeachDialog.test.tsx`

**Interfaces:**
- **Library and detail**: they use `PageHeader` with breadcrumbs ("Inbox / Documents / <file>").
  - Detail gains an "Imported into" line with the account (a link) and the counts from `GET /documents/{id}/imported`, and the unchecked `QuietNote`.
  - The review link goes to `/inbox?document=<id>`.
- **`PageViewer`**: the same behaviour (click a word, drag a box, highlight) on the SVG overlay. Pointer coordinates map through `getScreenCTM()`.
- **Every link** uses the new paths.

- [ ] **Step 1: Failing tests:**
  - `DocumentsPage.test.tsx` at `/inbox/documents/d-1`:
    - `the detail names the account it was imported into, with the counts`.
    - `the review link goes to the Inbox with the document`.
    - `breadcrumbs lead back to the library`.
  - `mapping.test.tsx`: `saving returns to the document`.
  - `TeachDialog.test.tsx`: `the outcome links to what PerFin learned`.
  - `highlight.test.ts` and the page viewer cases still pass: boxes are found by `data-testid="word-box"` on the `rect`.
  - `has no axe violations` on the library and the detail.
- [ ] **Step 2:** Run `npm test -- src/features/documents src/features/imports src/features/review`. Expected: no account line; the editor returns to `/documents`.
- [ ] **Step 3: Implement.** Then `grep -n "style=" frontend/src/features/documents` should find nothing.
- [ ] **Step 4:** Run the tests, `npm run lint` and `npm run typecheck`. Expected: pass.
- [ ] **Step 5:** Commit `feat(inbox): documents under the Inbox, each saying where its rows went`.

### Task 10: Transactions: filters in the URL, totals, the new grid (lane T)

**Files:**
- Create: `frontend/src/features/transactions/filters.ts`, `filters.test.ts`
- Modify: `features/transactions/TransactionsPage.tsx`, `cells.tsx` (the category cell becomes a chip with the provenance line; the inline select is removed; the suggestion keeps Accept), `RowActions.tsx` (uses `components/Menu`; its own positioning code goes), `BulkBar.tsx`, `transactions.css`, `TransactionsPage.test.tsx`, `TransactionActions.test.tsx`

**Interfaces:**
- **`filters.ts`**:
  - `fromSearch(search: Record<string, unknown>): TransactionFilters` and `toSearch(filters): Record<string, string>`, round-tripping the keys under "Search parameters" and leaving defaults out.
  - `totals(rows): { in: string; out: string; net: string; count: number }`, in decimal strings, through `lib/money`.
- **The page**:
  - Filters are read from and written to the URL (`replace`). The rail link (no search) clears them.
  - A search field (the first control after the heading; no character-key shortcut), then filter chips:
    - "Suggestions", with its count;
    - "No category", with its count;
    - the account select;
    - the category picker;
    - the date range.
  - The total row is visible when any filter is set: "In $x · Out $y · Net $z, of the N shown".
  - **Rows**: date, merchant (the display name, with the raw descriptor in small text when it differs), category chip (`categoryColourClass`, the provenance line from `provenance.ts`, and "<Category>?" plus Accept for a suggestion), account (hidden under 720 px), and amount.
  - The first cell is a `rowLink` button "Open <merchant>, <date>, <amount>". Up and Down move between rows (roving `tabindex` on the row link buttons), and Enter opens.
  - The row menu (`Menu`) has: "Change category…" (opens the drawer, Task 11), "Always categorise <merchant> as…", Edit, Split, Mark as transfer and Delete. It is gated as today.
- **`RowActions`**: has no CSSOM write of its own.

- [ ] **Step 1: Failing tests.**
  - **`filters.test.ts`**: round trip; unknown values ignored; `category=none`; totals with refunds and transfers (transfers counted in neither in nor out, with a note).
  - **`TransactionsPage.test.tsx`**:
    - `a filter change is written to the address`.
    - `the rail link clears the filters`.
    - `the total row sums the shown rows`.
    - `arrow keys move between rows and Enter opens the row`.
    - `the category cell shows its source and confidence`.
    - `a suggestion is a question with Accept`.
    - `the raw descriptor stays visible`.
    - `has no axe violations`.
  - **Stage 2 tests that pinned the inline select**: rewrite them in `TransactionActions.test.tsx` to go through the drawer or the bulk bar, and name them in the commit.
- [ ] **Step 2:** Run `npm test -- src/features/transactions`. Expected: filters not in the URL; no total row.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the tests, `npm run lint` and `npm run typecheck`. Expected: pass.
- [ ] **Step 5:** Commit `feat(transactions): filters in the address, a total for what is shown, and a grid that says why`.

### Task 11: Transactions: the drawer, and rules made in context (lane T)

**Files:**
- Create: `frontend/src/features/transactions/TransactionDrawer.tsx`, `TransactionDrawer.test.tsx`, `AlwaysSection.tsx`
- Modify: `TransactionsPage.tsx` (`tx` and `always` open the drawer), `AlwaysCategoriseDialog.tsx` (its preview, save and undo logic moves into `AlwaysSection`; the dialog wraps it), `AlwaysCategoriseDialog.test.tsx`, `transactions.css`, `optimistic.ts`
- Delete: `features/transactions/TeachPrompt.tsx` and the tests that cover it (named in the commit)

**Interfaces:**
- **`TransactionDrawer { transactionId: string; alwaysOpen?: boolean; onClose(): void }`**: it uses `components/Drawer` and holds, in the order of spec section 7:
  - the merchant, amount and date;
  - the account (a link to `/accounts/<id>`);
  - "Bank wrote";
  - "Came from": `/inbox/documents/<source_document_id>`, or "Entered by hand";
  - "Category by": `sourceLabel` and `confidenceText`;
  - `CategoryPicker` with `onCreate` for members and above, which opens `NewCategoryDialog`;
  - Accept for a suggestion;
  - `AlwaysSection` for teachable rows;
  - the actions Edit details (`TransactionDialog`), Split, Mark as transfer and Delete.
- **`AlwaysSection { transaction; categoryId; defaultChecked: boolean }`**:
  - **The first checkbox**, unticked unless `defaultChecked`, reads "Always use <category> for <merchant>". When it is ticked, the section calls `POST /rules/merchant/preview` and shows stage 2's three sentences.
  - **The second checkbox**, always unticked by default, reads "Also change the <n> existing transactions"; n is `uncategorised + guessed`. It is hidden when n is 0.
  - **Save** sends `POST /rules/merchant` with `apply_to_existing` equal to the second box.
  - **A toast** says "Rule saved", with Undo (revert). The job progress shows when existing rows change.
  - **Not teachable or shadowed**: stage 2's reason and a link to `/settings/rules/<id>`.
- **Choosing a category in the drawer** is `PATCH` with an optimistic `category_source: "person"`, and the toast has Undo.
- **Closing** removes `tx` and `always` from the URL, and Back closes. Focus returns to the row's link button.

- [ ] **Step 1: Failing tests** in `TransactionDrawer.test.tsx` (at `/transactions?tx=t-1`):
  - `opens from the address and shows where the row came from` (a document link).
  - `entered by hand when there is no document`.
  - `shows how the category was decided`.
  - `choosing a category is the person's choice and can be undone`.
  - `the always box is unticked and asks the server for counts when ticked`.
  - `existing rows change only when the second box is ticked` (the request body).
  - `?always=t-1 opens with the box ticked and nothing saved`.
  - `a row that cannot teach says why`.
  - `New category creates and selects`.
  - `a viewer sees no picker, no always and no new category`.
  - `Escape closes, the address loses tx and focus returns to the row`.
  - `has no axe violations`.

  `AlwaysCategoriseDialog.test.tsx` keeps passing through the dialog wrapper.
- [ ] **Step 2:** Run `npm test -- src/features/transactions`. Expected: there is no drawer.
- [ ] **Step 3: Implement**, and delete `TeachPrompt.tsx`. Then `grep -rn "TeachPrompt" frontend/src` should find nothing.
- [ ] **Step 4:** Run the tests, `npm run lint` and `npm run typecheck`. Expected: pass.
- [ ] **Step 5:** Commit `feat(transactions): one drawer per row: where it came from, why, and "always" with a true preview`.

### Task 12: Accounts: grouped list, net worth header, Investments sub-page (lane A)

**Files:**
- Create: `frontend/src/features/accounts/groups.ts`, `groups.test.ts`, `accounts.css`
- Modify: `features/accounts/AccountsHomePage.tsx` (the bridge becomes the page), `AccountsPage.tsx` (its list becomes `AccountGroups`; the file is kept for the export, or deleted when nothing imports it), `AccountsPage.test.tsx`, `AccountForms.tsx` (Add account creates an asset with a first valuation for `manual_asset`, as loans already do), `api.ts` (additions only), `features/investments/InvestmentsPage.tsx` (`?tab=` and `?account=` through `useTabParam` and `useSearchParam`), `investments/parts.tsx` (`AllocationBars` draws with `ProgressBar`; the `style` site goes), `investments.test.tsx`, `features/assets/AssetsPage.tsx` and `features/loans/LoansPage.tsx` (each is reduced to its create dialog or deleted, depending on whether `AccountForms` covers it; the bridge stops using them)

**Interfaces:**
- **`groups.ts`**:
  - `GROUPS`: `cash` (checking, savings, cash), `credit` (credit_card, line_of_credit), `investments` (brokerage, retirement), `loans` (loan, mortgage, manual_liability), `property` (manual_asset).
  - `groupAccounts(accounts) -> { id, label, total: string, accounts }[]`: closed accounts are left out; empty groups are left out.
  - `freshness(account, now): string`: "Balance from <date>" or "Your estimate, <month year>" for manual accounts.
- **`AccountsHomePage`**: a header with net worth (`GET /networth/latest`) as a `Stat` of display size, its change this month, and "Net worth over time" linking to `/reports/net-worth`. Then the groups as panels with totals and rows.
  - Each row links to `/accounts/<id>`.
  - "Closed (n)" is folded in a `<details>`.
  - `?group=` scrolls to that group and focuses its heading.
  - "Add account" is shown to those who may write.
- **Investments sub-page**: today's four tabs, with `activity` as the id of today's "Transactions" tab (labelled "Activity"), and the account filter in the URL.

- [ ] **Step 1: Failing tests.**
  - **`groups.test.ts`**: each type in its group; totals in decimal; closed accounts left out; freshness texts.
  - **`AccountsPage.test.tsx`** (at `/accounts`):
    - `groups with totals and net worth on top`.
    - `?group=loans focuses the loans group`.
    - `closed accounts are folded`.
    - `adding a manual asset creates the asset too`.
    - `a viewer sees no Add account`.
    - `has no axe violations`.
  - **`investments.test.tsx`** at `/accounts/investments`: `the tab and account are in the address`; `has no axe violations`.
- [ ] **Step 2:** Run `npm test -- src/features/accounts src/features/investments src/features/assets src/features/loans`. Expected: no groups.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the tests, `npm run lint` and `npm run typecheck`. Expected: pass.
- [ ] **Step 5:** Commit `feat(accounts): every account in one list grouped by kind, with net worth on top`.

### Task 13: The account hub: one page per account, tabs by type (lane A)

**Files:**
- Create: `frontend/src/features/accounts/hub.ts`, `hub.test.ts`, `HubTransactions.tsx`, `HubDocuments.tsx`, `AccountHub.test.tsx`
- Modify: `features/accounts/AccountHubPage.tsx` (the bridge becomes the page), `AccountDetailPage.tsx` (its Overview content is exported as `AccountOverview`; the page is deleted when nothing imports it), `BalanceChart.tsx` (chart classes), `DeleteAccount.tsx`, `DeleteAccount.test.tsx`, `features/investments/HoldingsTab.tsx`, `PerformanceTab.tsx`, `TransactionsTab.tsx`, `CostMethodCard.tsx` (each takes a fixed `account` as today), `features/loans/LoanDetailPage.tsx` (split into `LoanSchedule`, `LoanPayments`, `LoanRates` and `LoanPayoff` panels taking `loanId`; the page is deleted), `PaymentsTab.tsx`, `RateChangesTab.tsx`, `SimulatorTab.tsx`, `EditLoanDialog.tsx`, `loans.test.tsx`, `features/assets/AssetDetailPage.tsx` (becomes `AssetValuations` taking `assetId`), `assets.test.tsx`

**Interfaces:**
- **`hub.ts`**: `tabsFor(account, { loanId?, assetId? }): { id, label }[]`, by spec section 1.3's table; `defaultTab = "overview"`.
- **`AccountHubPage`**:
  - It has a `PageHeader` with breadcrumbs ("Accounts / <name>"), the balance as a `Stat`, freshness, and a `Menu` (Edit, Close or Reopen, Delete).
  - Tabs come through `useTabParam`.
  - The loan and the asset are found from `GET /loans` and `GET /assets` by `account_id`.
  - Deleting the loan on Schedule says "The account stays."
  - `HubTransactions` lists the latest 50 through `listTransactions({ account })`. Each row links to `/transactions?account=<id>&tx=<tx>`, and the list ends with "All transactions of this account".
  - `HubDocuments` lists `GET /documents?account=<id>`, linking to `/inbox/documents/<id>`.
  - "Add transaction" opens `TransactionDialog`, as today.

- [ ] **Step 1: Failing tests.**
  - **`hub.test.ts`**: the tabs per type.
  - **`AccountHub.test.tsx`** (at `/accounts/a-1`):
    - `a checking account has overview, transactions and documents`.
    - `transactions link to the drawer`.
    - `documents link to the Inbox`.
    - `has no axe violations`.
  - **`loans.test.tsx`**, its paths moved to `/accounts/<loan account>?tab=…`: schedule, payments, rate changes and payoff as today; `deleting the loan keeps the account`.
  - **`assets.test.tsx`**, at `/accounts/<asset account>?tab=valuations`: valuations as today.
  - **`investments.test.tsx`**: `a brokerage hub has holdings, activity and performance for that account`.
  - **`DeleteAccount.test.tsx`**: unchanged behaviour from the header menu.
- [ ] **Step 2:** Run `npm test -- src/features/accounts src/features/loans src/features/assets src/features/investments`. Expected: no tabs.
- [ ] **Step 3: Implement.** Delete the old detail pages once nothing imports them. Then `grep -rn "LoanDetailPage\|AssetDetailPage\|AccountDetailPage" frontend/src` should find nothing.
- [ ] **Step 4:** Run the tests, `npm run lint` and `npm run typecheck`. Expected: pass.
- [ ] **Step 5:** Commit `feat(accounts): one page per account with its transactions, documents and type's tabs`.

### Task 14: Plan: Subscriptions and Upcoming (lane P)

**Files:**
- Modify: `frontend/src/features/subscriptions/SubscriptionsPage.tsx` (figures as a `StatGroup`; uses `lib/useMediaQuery`; links to `/plan/subscriptions/<id>` and `/transactions?subscription=<id>`), `SubscriptionDetail.tsx` (breadcrumbs "Plan / Subscriptions / <name>" on narrow screens, where it is a page), `subscriptions.css`, `SubscriptionsPage.test.tsx`, `SubscriptionDetail.test.tsx`, `features/calendar/CalendarPage.tsx` (`useTabParam(["calendar", "recurring"], "calendar")`), `CalendarTab.tsx`, `RecurringTab.tsx`, `calendar.test.tsx`
- Delete: `features/subscriptions/useWide.ts`

**Interfaces:**
- **The Subscriptions page** keeps stage 2's content and order. Its header figures are `Stat`s. Its tab between Subscriptions and Bills stays as page tabs (`?kind=bills`), below the Plan tab bar.
- **Upcoming**: the calendar and the recurring list. The link "See subscriptions" points to `/plan/subscriptions`.
- **No `style` sites, and no colour literals.**

- [ ] **Step 1: Failing tests:**
  - `SubscriptionsPage.test.tsx` at `/plan/subscriptions`: stage 2's cases at the new path; `the per-month and per-year figures are a description list`; `has no axe violations`.
  - `SubscriptionDetail.test.tsx` at `/plan/subscriptions/s-1`: `links to its transactions`.
  - `calendar.test.tsx` at `/plan/upcoming`: `the tab is in the address`; the recurring cases from stage 2; `has no axe violations`.
- [ ] **Step 2:** Run `npm test -- src/features/subscriptions src/features/calendar`. Expected: old links and no tab parameter.
- [ ] **Step 3: Implement.** Then `grep -rn "useWide" frontend/src` should find nothing.
- [ ] **Step 4:** Run the tests, `npm run lint` and `npm run typecheck`. Expected: pass.
- [ ] **Step 5:** Commit `feat(plan): subscriptions and what is coming up, under Plan`.

### Task 15: Plan: Budgets and Goals (lane P)

**Files:**
- Modify: `frontend/src/features/budgets/BudgetsPage.tsx`, `BudgetDetailPage.tsx` (`ProgressBar`; the `style` site goes; breadcrumbs "Plan / Budgets / <name>"), `budgets.test.tsx`, `features/goals/GoalsPage.tsx` (`ProgressBar`; the `style` site goes), `goals.test.tsx`

**Interfaces:**
- **Write controls are gated.** New budget, edit and delete, and New goal, edit, contribute and delete, render only when `useCanWrite()` is true. This is the audit's viewer finding.
- **The budget lines use `Chip` with `categoryColourClass`.** Each line links to `/transactions?category=<id>&date_from=…&date_to=…`, the budget's period.
- **A goal row** shows "On pace for <month year>" when `progress.ts` gives a projection.

- [ ] **Step 1: Failing tests:**
  - `budgets.test.tsx` at `/plan/budgets` and `/plan/budgets/b-1`: `a viewer sees no New budget`; `an over-budget line is announced as over` (the progressbar's name includes "over"); `a line links to its transactions for the period`; `has no axe violations`.
  - `goals.test.tsx` at `/plan/goals`: `a viewer sees no New goal`; `has no axe violations`.
- [ ] **Step 2:** Run `npm test -- src/features/budgets src/features/goals`. Expected: viewers see the buttons.
- [ ] **Step 3: Implement.** Then `grep -n "style=" frontend/src/features/budgets frontend/src/features/goals` should find nothing.
- [ ] **Step 4:** Run the tests, `npm run lint` and `npm run typecheck`. Expected: pass.
- [ ] **Step 5:** Commit `feat(plan): budgets and goals in the new look, write buttons only for who may write`.

### Task 16: Reports: the four views under the section tab bar (lane R)

**Files:**
- Modify: `frontend/src/features/reports/ReportsPage.tsx` (its own `Tabs` is removed and the section tab bar selects the view; `state.tab` still comes from `searchState`), `ControlBar.tsx`, `ReportCharts.tsx` (series through the chart classes; `reports.css` loses its private `--report-series-*`), `reports.css`, `FlowView.tsx`, `CashflowView.tsx`, `TrendsView.tsx`, `links.ts`, `parts.tsx`, `reports.test.tsx`, `testing.ts`

**Interfaces:**
- **The view still comes from `?tab=`.** The section tab bar's items for the four views keep `period`, `from`, `to`, `account` and `pending` (Decision 5).
- **Headline figures are `StatGroup`s.** Each chart keeps its table and its CSV download.
- **Category and account rows link** to `/transactions?…`, as today.

- [ ] **Step 1: Failing tests** in `reports.test.tsx`:
  - `the section tab bar switches the view and keeps the period and account`.
  - `there is one tab list, not two`.
  - `charts carry series classes, not colours`.
  - `has no axe violations`, per view.
- [ ] **Step 2:** Run `npm test -- src/features/reports`. Expected: two tab lists.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the tests, `npm run lint` and `npm run typecheck`. Expected: pass.
- [ ] **Step 5:** Commit `feat(reports): spending, income, cash flow and trends under one tab bar`.

### Task 17: Reports: Net worth and Tax (lane R)

**Files:**
- Modify: `frontend/src/features/networth/NetWorthPage.tsx` (breakdown rows link to `/accounts/<id>`; chart classes), `networth.test.tsx`, `chartData.ts` if needed, `features/tax/TaxYearsPage.tsx`, `TaxYearPage.tsx` (breadcrumbs "Reports / Tax / <year>"; links to `/reports/tax/<year>`, documents to `/inbox/documents/<id>`, and tax hints to `/settings/categories?tab=tax`), `ExportCard.tsx`, `DeleteTaxYear.tsx`, `tax.css`, `tax.test.tsx`

- [ ] **Step 1: Failing tests:**
  - `networth.test.tsx` at `/reports/net-worth`: `every breakdown row links to its account`; `has no axe violations`.
  - `tax.test.tsx` at `/reports/tax` and `/reports/tax/2025`: today's cases; `a document link goes to the Inbox`; `tax hints link to Settings`; `has no axe violations`.
- [ ] **Step 2:** Run `npm test -- src/features/networth src/features/tax`. Expected: no links.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the tests, `npm run lint` and `npm run typecheck`. Expected: pass.
- [ ] **Step 5:** Commit `feat(reports): net worth and tax years under Reports, linked to their accounts and documents`.

### Task 18: Settings: the side list, the index, the pages in the new look (lane S)

**Files:**
- Create: `frontend/src/features/settings/SettingsNav.tsx`, `SettingsNav.test.tsx`, `settings.css`
- Modify: `features/settings/SettingsLayout.tsx` (side list from `routeMap.SETTINGS_GROUPS`; `SectionHeadingContext` provided; `SettingsPage` uses `PageHeader` with breadcrumbs "Settings / <page>"; the index shows the groups as cards, with the intro sentence from spec section 1.3), `SettingsAdditions.test.tsx`, `GeneralPage.tsx` (title "Name and currency"), `HouseholdMembersPage.tsx` (title "Members"), and every settings page's test where its title changed (named in the commit)

**Interfaces:**
- **`SettingsNav`**: from 1024 px it is a `nav aria-label="Settings"` of groups with headings and links, with the current page marked `aria-current`. Below 1024 px it is hidden, and the index page is the list. "Users" shows only for an instance administrator.

- [ ] **Step 1: Failing tests:**
  - `SettingsNav.test.tsx`: `lists the six groups in order`; `marks the current page`; `Users only for instance administrators`; `has no axe violations`.
  - `SettingsAdditions.test.tsx`: `the index groups the pages and states where rules are made`.
  - The renamed page titles.
- [ ] **Step 2:** Run `npm test -- src/features/settings`. Expected: no side list.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the tests, `npm run lint` and `npm run typecheck`. Expected: pass.
- [ ] **Step 5:** Commit `feat(settings): one grouped list of settings; two "Household" pages get distinct names`.

### Task 19: Settings, Categorisation: categories with tax hints, rules, what was learned (lane S)

**Files:**
- Modify: `frontend/src/features/categories/CategoriesSettingsPage.tsx` (the bridge becomes the page: tabs Categories, Payees, Tags and "Tax hints" through `useTabParam`), `CategoriesPage.tsx` (tab content only; role checks; tree indent through `indent-1` … `indent-3` classes, so the `style` site goes; "New category" uses `NewCategoryDialog`), `MergeDialog.tsx`, `categories.test.tsx`, `features/tax/CategoryMapPage.tsx` (tab content, no page header), `features/rules/RulesPage.tsx`, `RuleEditorPage.tsx` (breadcrumbs "Settings / Rules / <name>"), `RuleForm.tsx`, `RuleTestPanel.tsx`, `RuleTransfer.tsx`, `RulesPage.test.tsx`, `RuleTransfer.test.tsx`, `features/imports/LearnedPage.tsx`, `learned.ts`, `LearnedPage.test.tsx` (links to `/transactions?always=<id>` and `/inbox/documents/<id>`)

**Interfaces:**
- **Categories**: add, rename, merge and delete render only for roles the API allows (member and above for create and update, as `authz_policy.py` says; read the policy lines for merge and delete). Viewers see the tree only.
- **The rules list and the learned page** say on top: "Rules are made where you work: in a transaction's panel or from an Inbox card. Here you can change their order, edit or undo them."
- **Every link** uses the new paths.

- [ ] **Step 1: Failing tests:**
  - `categories.test.tsx` at `/settings/categories`: `the four tabs are in the address`; `tax hints are a tab`; `a viewer sees no write buttons`; `indentation without a style attribute`; `has no axe violations`.
  - `RulesPage.test.tsx` at `/settings/rules`: today's cases at the new path; `the intro says where rules are made`; `has no axe violations`.
  - `LearnedPage.test.tsx` at `/settings/learned`: `Make it a rule opens the drawer link`; `has no axe violations`.
- [ ] **Step 2:** Run `npm test -- src/features/categories src/features/rules src/features/imports/LearnedPage.test.tsx src/features/tax`. Expected: no tax tab; viewers see buttons.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the tests, `npm run lint` and `npm run typecheck`. Expected: pass.
- [ ] **Step 5:** Commit `feat(settings): categories with their tax hints, rules and what PerFin learned, in one group`.

### Task 20: Command palette (lane K)

**Files:**
- Create: `frontend/src/app/commands.ts`, `commands.test.ts`, `CommandPalette.test.tsx`, `app/styles/palette.css`
- Modify: `frontend/src/app/CommandPalette.tsx` (the Start placeholder)

**Interfaces:**
- **`commands.ts`**:
  - `staticCommands(isInstanceAdmin: boolean): Command[]` builds from `routeMap` (sections, tabs and settings pages).
  - `accountCommands(accounts): Command[]` gives "Account: <name>", going to `/accounts/<id>`.
  - `rank(commands, query)`: case-insensitive and accent-insensitive. Prefix matches rank above word-start matches, which rank above substring matches. At most 12 results.
- **`CommandPalette`**:
  - Ctrl+K or Cmd+K toggles it. It does not open while focus is in a text field, unless the modifier is held.
  - It is a `Dialog` with a combobox input (`aria-expanded`, `aria-controls`, `aria-activedescendant`) and a listbox.
  - Up and Down move, Enter goes, Escape closes and returns focus.
  - It reads accounts from the query cache (`queryRoots.accounts`) and never fetches.
  - Empty: "No page or account matches."

- [ ] **Step 1: Failing tests:**
  - `commands.test.ts`: ranking, accents and the cap.
  - `CommandPalette.test.tsx`: `Ctrl+K opens and Escape closes, focus returns`; `typing filters and Enter navigates`; `accounts already loaded are listed`; `the combobox pattern attributes`; `has no axe violations`.
- [ ] **Step 2:** Run `npm test -- src/app/CommandPalette.test.tsx src/app/commands.test.ts`. Expected: the placeholder renders nothing.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the tests, `npm run lint` and `npm run typecheck`. Expected: pass.
- [ ] **Step 5:** Commit `feat(ui): a command palette to go to any page or account`.

### Task 21: Cross-lane integration (Final)

**Files:**
- Create: `frontend/src/app/oldLinks.test.ts`
- Modify: `frontend/eslint.config.js` (the `no-restricted-syntax` rule for `JSXAttribute[name.name='style']`), `features/dashboard/panels.tsx` (import `orderCards` from `inbox/order.ts` instead of its copy), and whatever a red test names, in any lane's file

- [ ] **Step 1: Failing tests:**
  - `oldLinks.test.ts`: reads every `src/features/**/*.tsx` and fails on any `to="/…"`, `to: "/…"` or `href="/…"` that starts with a `REDIRECTS[].from`, naming the file and line.
  - Lint: run with the new rule.
- [ ] **Step 2:** Run `npm test` and `npm run lint` on the merged lanes. Fix in the owning file; each fix gets a test beside the lane's tests.
- [ ] **Step 3: Checks by grep.** Each must come back empty:
  - Bridges left: `grep -rn "Stage 3 bridge" frontend/src`.
  - Deleted pages still imported: `grep -rn "DashboardPage\|UploadPage\|TeachPrompt\|useWide\|shellRoutes" frontend/src`.
  - Inline styles: `grep -rn "style={" frontend/src`.
  - Colour literals outside tokens: `grep -rnE "#[0-9a-fA-F]{6}" frontend/src --include=*.css | grep -v tokens.css`. Recharts tick text uses classes.
- [ ] **Step 4: Review Focus 3, by hand in a test.** Add to `src/features/inbox/InboxPage.test.tsx`, with a viewer session: `the badge, Home's needs-you and the Inbox show the same number`, all three rendered from one mocked summary and card set.
- [ ] **Step 5:** Run `npm run lint`, `npm run typecheck`, `npm test`, `npm run build`, `npm run check:bundle` and, in the container, `make check`. Expected: pass. Record the bundle figures; a figure over budget goes to the coordinator with the measurements, and no budget is raised here.
- [ ] **Step 6:** Commit `test(ui): the lanes together: old links, no inline styles, one count, bundle budget`.

### Task 22: Accessibility pass and route audit (Final)

**Files:**
- Create: `frontend/src/app/routeAudit.test.tsx`
- Modify: whatever it shows, in any file

- [ ] **Step 1: Failing tests** in `routeAudit.test.tsx`:
  - **Every new route**, for `NEW_ROUTES` with fixture ids and a fully mocked API (every list endpoint returning one small row, from a shared fixture in the test file): `renders a heading`, `has exactly one h1`, `has no axe violations with data-theme light`, `… with data-theme dark`, `main is labelled by its h1 or the section h1`.
  - **Keyboard journeys**, with `userEvent`:
    - from Home, Tab reaches the rail, Enter on Inbox, then Tab reaches the first card's action;
    - on Transactions, Tab to the search, type, Tab to the first row, Down, Enter opens the drawer, Escape returns to the row;
    - Ctrl+K, "budg", Enter lands on `/plan/budgets`.
  - **Phone width** (`matchMedia` stubbed for `max-width: 819px`): the bottom bar holds the six sections, and the account menu holds Settings.
  - **Target size**: every `.chip`, rail link and icon button has a CSS `min-height` of at least 24px. Read the rule from `components.css` and `layout.css` with `node:fs`, because jsdom does no layout.
- [ ] **Step 2:** Run it. Fix each finding in the owning file.
- [ ] **Step 3:** Run the whole frontend suite. Expected: pass.
- [ ] **Step 4:** Commit `test(a11y): every page has one h1, no axe violations in either theme, and keyboard journeys work`.

### Task 23: Documentation (Final)

**Files:**
- Create: `docs/USING.md` ("Finding your way around": the six sections and Settings, what lives where, the old-to-new table in plain words, the Inbox, the drawer and "always", keyboard shortcuts, themes, using it on a phone). Text only; the repository keeps no screenshots.
- Modify:
  - `mkdocs.yml`: `USING.md` under "Running PerFin", after the first page; `superpowers/` stays excluded.
  - `docs/DEVELOPMENT.md`: "Plug-in points" now means a page is added to `app/routeTree.tsx` and `routeMap.ts`; the styling rules (tokens, no `style` prop, SVG geometry, chart classes); `check:bundle`; `axe` in page tests.
  - `docs/REPO_STRUCTURE.md`.
  - `docs/INGESTION.md` and `docs/CATEGORISATION.md`: paths `/inbox`, `/inbox/documents`, `/settings/learned`, `/settings/rules`; "Check again" now lives on Settings, What PerFin learned.
  - `docs/API.md`: `GET /inbox/summary`.
  - `docs/security/CONTROLS.md`: every moved or deleted frontend path. Check each `frontend/src` citation with `grep -n "frontend/src" docs/security/CONTROLS.md` and `ls` each path. Add a line on the CSP rule and its ESLint enforcement, and on the fonts being self-hosted.
  - `docs/security/what-leaves-the-host.md`: fonts are served by PerFin; still nothing leaves.
  - `docs/ROADMAP.md`.

- [ ] **Step 1:** Write the pages.
- [ ] **Step 2:** `grep -rnE '\(/(review|budgets|goals|loans|assets|investments|net-worth|calendar|rules|subscriptions)\b|/documents/(library|learned)' docs --include=*.md | grep -v superpowers` finds only the old-to-new table in `USING.md` and `UPGRADING.md`.
- [ ] **Step 3:** `mkdocs build --strict` (in the docs venv). Expected: pass.
- [ ] **Step 4:** Commit `docs: finding your way in the new layout; paths, controls and development notes updated`.

### Task 24: Changelog, upgrade note, full gates (Final)

**Files:**
- Modify: `CHANGELOG.md` (`## [Unreleased]`), `docs/UPGRADING.md` (`### 1.0.0-rc.11` under "Notes for specific releases")

- [ ] **Step 1: `UPGRADING.md` "1.0.0-rc.11".**
  - No migration, no new setting, no new container. One new read-only endpoint.
  - Every old address redirects, so bookmarks keep working. Give the table in short.
  - The dashboard layout you customised is no longer used. Home is one fixed page, and nothing was deleted from the database.
  - Fonts are now served by PerFin itself; a strict proxy needs nothing new.
  - Browsers cache the old bundle until reload, and the service worker serves only hashed files, so one reload picks up the new UI.
- [ ] **Step 2: `CHANGELOG.md`.** Added, Changed and Removed entries in the file's voice, one per thing a person can notice:
  - the sections and Settings;
  - the Inbox and its count;
  - the drawer and "always";
  - the account hub;
  - Plan and Reports;
  - the command palette;
  - light and dark;
  - the phone layout;
  - the redirects;
  - customisation and the inline category select removed;
  - the bundle figures before and after.
- [ ] **Step 3: Full gates.** Run `python scripts/check_spdx.py`; then, in the container, `make check` (backend and frontend, including `check:bundle`); then `mkdocs build --strict`. Record what could not be run here, for CI before the tag.
- [ ] **Step 4:** Commit `docs: changelog and the rc.11 upgrade note for the redesign`.

### Task 25: Visual check against the mock-up and route audit (coordinator)

**Files:** none in the repository. This task is reserved for the coordinator. It runs after Task 24 and before `v1.0.0-rc.11` is tagged.

- [ ] **Step 1:** Run the dev stack with the synthetic seed (`scripts/dev.sh`). Open each section beside the mock-up at 1280 px, at 900 px and at 390 px, in light and in dark. Compare the layout, the rail and bottom bar, the type, the spacing, the panels, the chips, the Inbox cards, the drawer and the tab bars. List each difference, and decide for each: fix now (in the owning file, with a test where behaviour is involved), accept and note it in the pull request, or leave it as a deliberate departure from the spec (sections 1.5 and 2.1).
- [ ] **Step 2:** Walk the route map by hand: every old URL in the address bar lands on its new home, with search and hash kept; every rail, tab and settings link works; Back and Forward behave as expected around the drawer and the redirects.
- [ ] **Step 3:** Load the production build under the real CSP (`perfin` image or `vite preview` behind the dev Caddy). The console shows no CSP violation, and fonts and charts render.
- [ ] **Step 4:** Do a keyboard-only pass of the three Task 22 journeys, and a screen reader spot check (VoiceOver) of the rail, the Inbox badge, the drawer and the palette.
- [ ] **Step 5:** Put into the pull request body: the differences and their decisions, the bundle figures before and after, the route audit result, and anything not run.
