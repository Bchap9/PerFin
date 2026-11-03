# Vision and scope

## One sentence

A self-hosted personal finance system that a security-conscious household can
trust with everything: bank and card statements, investments, loans, receipts
and tax documents, with ingestion smart enough that keeping it current takes
minutes a month rather than hours.

## Who it is for

- **The self-hoster.** Runs a home server or small VPS, already uses Authentik
  (or another OIDC provider), and wants finance data to stay on their own
  hardware. Comfortable with Docker Compose and a reverse proxy.
- **The household.** One or two primary users, possibly a partner with
  read-only or partial access, possibly a dependant. Multiple people, one
  shared financial picture with per-account visibility controls.
- **The tax-time self.** Six months from now, you need every 1099, W-2,
  charitable receipt and HSA statement for a given year in one place.

## Feature inventory

### Accounts and ledger
- Account types: checking, savings, credit card, cash, brokerage, retirement,
  loan, mortgage, line of credit, manual asset (property, vehicle, other),
  manual liability.
- Multi-currency with a base currency and historical FX rates.
- Transactions with splits, transfers (paired legs recognised automatically),
  payees, categories, tags, notes, attachments (receipt linked to transaction).
- Scheduled and recurring transaction detection (subscriptions, salary, bills).
- Rules engine: conditions on payee/memo/amount/account produce category,
  tags, rename, split, or flag for review. Rules are ordered, testable and
  exportable.

### Spending and budgets
- Category hierarchy, user-defined, with a sensible default set.
- Budgets: monthly envelope, rolling, and annual targets; forecast vs actual.
- Trends: month over month, year over year, per category and per payee.
- Cash-flow calendar: upcoming bills and expected income.

### Savings and goals
- Goals attached to one or more accounts with target amount and date.
- Progress projections based on observed contribution rate.

### Investments
- Holdings per account: lots, cost basis, dividends, splits, reinvestments.
- Transaction types: buy, sell, dividend, interest, fee, transfer in/out,
  corporate action.
- Valuation via pluggable price providers (manual entry always available;
  external feeds are opt-in and clearly marked as off-host calls).
- Performance: time-weighted and money-weighted returns, allocation by asset
  class, sector and account, realised and unrealised gains.

### Loans
- Mortgage, auto, student, personal; fixed and variable rate.
- Amortisation schedule, extra-payment modelling, payoff date projections.
- Statement import updates principal/interest split and escrow.

### Net worth
- Daily snapshots of every account balance and holding valuation.
- History, breakdown by asset/liability class, and what-if projections.

### Documents and ingestion
- Upload, drop-folder and email intake (see INGESTION.md).
- Automatic classification: statement, receipt, invoice, tax form, insurance,
  payslip, other.
- Automatic extraction: institution, account, period, balances, transactions,
  totals, merchant, tax year, form type.
- Deduplication across intake paths and overlapping statements.
- Review queue for anything below confidence thresholds, with one-click fixes
  that teach the system (new rule, new parser fingerprint).
- Filing: a managed, encrypted document store organised by entity and date,
  with full-text search over extracted text.

### Tax vault
- Tax year as a first-class container.
- Checklist of expected documents per year (learned from previous years,
  editable), with "received / missing" status.
- Deductible-expense tagging on transactions with yearly roll-up.
- Export: zip of originals plus a summary CSV/PDF for an accountant.

### Platform
- Dashboard with configurable widgets.
- Notifications: in-app, email, webhook, ntfy/Gotify/Matrix adapters later.
- Full export (JSON + CSV + documents) and import. No lock-in.
- Audit log of every write and every login.
- Backups: documented procedure, encrypted archives, restore test script.
- API with personal access tokens (scoped) for scripting.

## Non-goals (for now)

- Direct bank aggregation (Plaid, Yodlee, open banking). Statement ingestion is
  the primary model; a bank-sync plugin interface may come later.
- Bill payment or any money movement. PerFin is read-and-record only.
- Mobile native apps. The web UI will be responsive; a PWA manifest is enough.
- Being a general document management system. Documents are kept because they
  are financial records.

## Principles that settle arguments

1. **Untrusted input stays untrusted.** Every file, email and OIDC claim is
   hostile until validated. Parsing happens in a sandbox.
2. **Confidence or review.** Automation never silently writes low-confidence
   data. It writes to the review queue.
3. **Off-host is opt-in.** Nothing leaves the server unless the user turned on a
   specific integration and can see what it sends.
4. **Boring where it counts.** Postgres, session cookies, well-known libraries.
   Novelty goes into the ingestion smarts, not the security plumbing.
5. **Everything exportable.** If PerFin disappears tomorrow, the data walks out
   in open formats.
