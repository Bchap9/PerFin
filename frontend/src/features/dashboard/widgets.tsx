// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The dashboard widgets. Each loads its own data, so one failing widget
 * does not blank the page, and each is a labelled region (its card title).
 */
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useMemo } from "react";

import { Badge } from "../../components/Badge";
import { Card } from "../../components/Card";
import { EmptyState } from "../../components/EmptyState";
import { MoneyText } from "../../components/MoneyText";
import { Table, type TableColumn } from "../../components/Table";
import { formatDate, formatMonth, todayIsoDate } from "../../lib/dates";
import { budgetKeys, getBudgetReport, listBudgets } from "../budgets/api";
import { barWidth, lineStatus } from "../budgets/report";
import { QueryState } from "../planning/PlanningPage";
import { nameLookup, useAccounts, useCategories } from "../planning/queries";
import { listTaxYears, openMissing, reminderText } from "../tax/api";
import { listGoals, goalKeys } from "../goals/api";
import { goalBarWidth } from "../goals/progress";
import { getCalendar, recurringKeys } from "../calendar/api";
import { getHoldings, investmentKeys } from "../investments/api";
import { AllocationBars, GainText, titleCase } from "../investments/parts";
import { listLoans, loanKeys } from "../loans/api";
import { getLatest, netWorthKeys } from "../networth/api";
import { dashboardKeys, getCashflow, getOpenReviewCount, getRecentTransactions, getSpendingByCategory } from "./api";
import { accountTotals, monthRange } from "./helpers";
import type { WidgetId } from "./layout";

const MAX_BUDGETS = 3;

export function CashflowCard({ from, to }: { from: string; to: string }) {
  const query = useQuery({ queryKey: dashboardKeys.cashflow(from, to), queryFn: () => getCashflow(from, to) });
  const report = query.data;
  return (
    <Card title={`Cash flow, ${formatMonth(from, { style: "long" }) ?? from}`}>
      <QueryState loading={query.isPending} error={query.error} label="cash flow">
        {report === undefined ? null : (
          <dl className="dashboard-figures">
            <div>
              <dt>Money in</dt>
              <dd><MoneyText amount={report.inflow.amount} currency={report.currency} tone="signed" /></dd>
            </div>
            <div>
              <dt>Money out</dt>
              <dd><MoneyText amount={report.outflow.amount} currency={report.currency} tone="none" /></dd>
            </div>
            <div>
              <dt>Net</dt>
              <dd><MoneyText amount={report.net.amount} currency={report.currency} tone="signed" /></dd>
            </div>
          </dl>
        )}
      </QueryState>
    </Card>
  );
}

export function SpendingCard({ from, to }: { from: string; to: string }) {
  const query = useQuery({ queryKey: dashboardKeys.spending(from, to), queryFn: () => getSpendingByCategory(from, to) });
  const report = query.data;
  const items = useMemo(() => (report?.items ?? []).slice(0, 8), [report]);
  const largest = items[0]?.amount.amount ?? "0";
  return (
    <Card title="Spending by category">
      <QueryState loading={query.isPending} error={query.error} label="spending">
        {report === undefined ? null : items.length === 0 ? (
          <p className="muted">No spending this month yet.</p>
        ) : (
          <ul className="planning-list" aria-label="Spending by category">
            {items.map((item) => (
              <li key={item.key ?? "none"} className="bar-row">
                <span>{item.name ?? "Uncategorised"}</span>
                <span className="bar" aria-hidden="true">
                  <span className="bar__fill" style={{ width: `${String(barWidth(item.amount.amount, largest))}%` }} />
                </span>
                <MoneyText amount={item.amount.amount} currency={report.currency} tone="none" />
              </li>
            ))}
          </ul>
        )}
      </QueryState>
    </Card>
  );
}

function BudgetStatus({ budgetId, name }: { budgetId: string; name: string }) {
  const query = useQuery({ queryKey: budgetKeys.report(budgetId, null, false), queryFn: () => getBudgetReport(budgetId, null, false) });
  const report = query.data;
  const watched = (report?.lines ?? []).filter((line) => lineStatus(line, line.kind) !== "ok").slice(0, 4);
  return (
    <li>
      <Link to="/budgets/$budgetId" params={{ budgetId }}>
        {name}
      </Link>
      {report === undefined ? (
        query.error === null ? null : <span className="muted"> Unavailable</span>
      ) : (
        <>
          {" "}
          <span className="muted">
            spent <MoneyText amount={report.expense_totals.actual.amount} currency={report.currency} tone="none" /> of{" "}
            <MoneyText amount={report.expense_totals.available.amount} currency={report.currency} tone="none" />
          </span>
          <span className="bar" role="img" aria-label={`${name}: ${String(Math.round(barWidth(report.expense_totals.actual.amount, report.expense_totals.available.amount)))} percent spent`}>
            <span
              className={lineStatus(report.expense_totals) === "over" ? "bar__fill bar__fill--over" : "bar__fill"}
              style={{ width: `${String(barWidth(report.expense_totals.actual.amount, report.expense_totals.available.amount))}%` }}
            />
          </span>
          {watched.length === 0 ? null : (
            <ul className="planning-list">
              {watched.map((line) => (
                <li key={line.category_id}>
                  {line.name ?? "Unknown"}:{" "}
                  <Badge tone={lineStatus(line, line.kind) === "over" ? "danger" : "warning"}>
                    {lineStatus(line, line.kind) === "over" ? "Over budget" : "Forecast over"}
                  </Badge>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </li>
  );
}

export function BudgetsCard() {
  const query = useQuery({ queryKey: budgetKeys.list, queryFn: listBudgets });
  const budgets = (query.data ?? []).slice(0, MAX_BUDGETS);
  return (
    <Card title="Budgets" actions={<Link to="/budgets">All budgets</Link>}>
      <QueryState loading={query.isPending} error={query.error} label="budgets">
        {budgets.length === 0 ? (
          <p className="muted">No budgets yet. <Link to="/budgets">Create one</Link>.</p>
        ) : (
          <ul className="planning-list">
            {budgets.map((budget) => (
              <BudgetStatus key={budget.id} budgetId={budget.id} name={budget.name} />
            ))}
          </ul>
        )}
      </QueryState>
    </Card>
  );
}

export function RecentCard() {
  const query = useQuery({ queryKey: dashboardKeys.recent, queryFn: () => getRecentTransactions() });
  const categories = useCategories();
  const categoryName = nameLookup(categories.data);
  type Row = NonNullable<typeof query.data>["items"][number];
  const columns: TableColumn<Row>[] = [
    { id: "date", header: "Date", cell: (row) => formatDate(row.posted_date, { style: "short" }) ?? row.posted_date },
    { id: "payee", header: "Description", rowHeader: true, cell: (row) => row.raw_payee ?? row.memo ?? "Transaction" },
    { id: "category", header: "Category", cell: (row) => (row.category_id === null ? <span className="muted">Uncategorised</span> : categoryName(row.category_id)) },
    { id: "amount", header: "Amount", align: "end", cell: (row) => <MoneyText amount={row.amount.amount} currency={row.amount.currency} /> },
  ];
  return (
    <Card title="Recent transactions" actions={<Link to="/transactions">All transactions</Link>}>
      <QueryState loading={query.isPending} error={query.error} label="transactions">
        <Table
          caption="Latest transactions"
          captionHidden
          columns={columns}
          rows={query.data?.items ?? []}
          rowKey={(row) => row.id}
          maxHeight="none"
          density="compact"
          empty={<EmptyState title="No transactions yet" headingLevel={3} />}
        />
      </QueryState>
    </Card>
  );
}

export function ReviewCard() {
  const query = useQuery({ queryKey: dashboardKeys.review, queryFn: getOpenReviewCount });
  const data = query.data;
  return (
    <Card title="Needs review" actions={<Link to="/review">Open review</Link>}>
      <QueryState loading={query.isPending} error={query.error} label="review items">
        {data === undefined ? null : data.count === 0 ? (
          <p>Nothing is waiting for you.</p>
        ) : (
          <p>
            <Badge tone="warning">{data.more ? `${String(data.count)}+` : String(data.count)}</Badge>{" "}
            {data.count === 1 && !data.more ? "item needs" : "items need"} a decision.
          </p>
        )}
      </QueryState>
    </Card>
  );
}

export function TaxCard() {
  const query = useQuery({ queryKey: dashboardKeys.tax, queryFn: listTaxYears });
  const missing = openMissing(query.data ?? []);
  return (
    <Card title="Tax documents" actions={<Link to="/tax">Open tax</Link>}>
      <QueryState loading={query.isPending} error={query.error} label="tax years">
        {missing.length === 0 ? (
          <p>No tax documents are missing.</p>
        ) : (
          <ul>
            {missing.map((entry) => (
              <li key={entry.year}>
                <Link to="/tax/$year" params={{ year: String(entry.year) }}>
                  {String(entry.year)}
                </Link>
                : {reminderText(entry.missing)}
              </li>
            ))}
          </ul>
        )}
      </QueryState>
    </Card>
  );
}

export function AccountsCard() {
  const query = useAccounts();
  const totals = useMemo(() => accountTotals(query.data ?? []), [query.data]);
  const open = (query.data ?? []).filter((account) => !account.is_closed);
  return (
    <Card title="Accounts" actions={<Link to="/accounts">All accounts</Link>}>
      <QueryState loading={query.isPending} error={query.error} label="accounts">
        {open.length === 0 ? (
          <p className="muted">No accounts yet.</p>
        ) : (
          <>
            {totals.map((total) => (
              <dl key={total.currency} className="dashboard-figures" aria-label={`Totals in ${total.currency}`}>
                <div>
                  <dt>Assets ({total.currency})</dt>
                  <dd><MoneyText amount={total.assets} currency={total.currency} tone="none" /></dd>
                </div>
                <div>
                  <dt>Liabilities ({total.currency})</dt>
                  <dd><MoneyText amount={total.liabilities} currency={total.currency} tone="none" /></dd>
                </div>
              </dl>
            ))}
            <ul className="planning-list" aria-label="Accounts">
              {open.slice(0, 8).map((account) => (
                <li key={account.id} className="planning-list__item">
                  <span>{account.nickname ?? account.name}</span>
                  <MoneyText amount={account.balance.amount} currency={account.balance.currency} tone="none" />
                </li>
              ))}
            </ul>
          </>
        )}
      </QueryState>
    </Card>
  );
}

export function NetWorthCard() {
  const query = useQuery({ queryKey: netWorthKeys.latest, queryFn: getLatest });
  const latest = query.data;
  return (
    <Card title="Net worth" actions={<Link to="/net-worth">Net worth</Link>}>
      <QueryState loading={query.isPending} error={query.error} label="net worth">
        {latest === undefined ? null : (
          <dl className="dashboard-figures">
            <div>
              <dt>Net worth</dt>
              <dd><MoneyText amount={latest.net.amount} currency={latest.currency} tone="signed" /></dd>
            </div>
            <div>
              <dt>Assets</dt>
              <dd><MoneyText amount={latest.assets.amount} currency={latest.currency} tone="none" /></dd>
            </div>
            <div>
              <dt>Liabilities</dt>
              <dd><MoneyText amount={latest.liabilities.amount} currency={latest.currency} tone="none" /></dd>
            </div>
          </dl>
        )}
      </QueryState>
    </Card>
  );
}

const MAX_GOALS = 4;

export function GoalsCard() {
  const query = useQuery({ queryKey: goalKeys.list(false), queryFn: () => listGoals(false) });
  const goals = (query.data ?? []).slice(0, MAX_GOALS);
  return (
    <Card title="Goals" actions={<Link to="/goals">All goals</Link>}>
      <QueryState loading={query.isPending} error={query.error} label="goals">
        {goals.length === 0 ? (
          <p className="muted">No goals yet. <Link to="/goals">Set one</Link>.</p>
        ) : (
          <ul className="planning-list">
            {goals.map((goal) => (
              <li key={goal.id}>
                <span className="planning-list__item">
                  <span>{goal.name}</span>
                  <span className="muted">
                    <MoneyText amount={goal.progress.contributed.amount} currency={goal.progress.contributed.currency} tone="none" /> of{" "}
                    <MoneyText amount={goal.target.amount} currency={goal.target.currency} tone="none" />
                  </span>
                </span>
                <span className="bar" role="img" aria-label={`${goal.name}: ${String(Math.round(goalBarWidth(goal)))} percent of the target`}>
                  <span className="bar__fill" style={{ width: `${String(goalBarWidth(goal))}%` }} />
                </span>
              </li>
            ))}
          </ul>
        )}
      </QueryState>
    </Card>
  );
}

const BILL_DAYS = 30;
const MAX_BILLS = 6;

export function UpcomingBillsCard() {
  const query = useQuery({ queryKey: recurringKeys.calendar(BILL_DAYS), queryFn: () => getCalendar(BILL_DAYS) });
  const bills = (query.data?.days ?? [])
    .flatMap((day) => day.events.filter((event) => event.kind === "bill").map((event) => ({ on: day.on, event })))
    .slice(0, MAX_BILLS);
  return (
    <Card title="Upcoming bills" actions={<Link to="/calendar">Calendar</Link>}>
      <QueryState loading={query.isPending} error={query.error} label="upcoming bills">
        {bills.length === 0 ? (
          <p className="muted">No bills are due in the next {BILL_DAYS} days.</p>
        ) : (
          <ul className="planning-list" aria-label="Upcoming bills">
            {bills.map(({ on, event }) => (
              <li key={`${event.schedule_id}-${on}`} className="planning-list__item">
                <span>
                  {event.name} <span className="muted">{formatDate(on, { style: "short" }) ?? on}</span>
                </span>
                <MoneyText amount={event.amount.amount} currency={event.amount.currency} tone="none" />
              </li>
            ))}
          </ul>
        )}
      </QueryState>
    </Card>
  );
}

export function HoldingsCard() {
  const query = useQuery({ queryKey: investmentKeys.holdings(""), queryFn: () => getHoldings("") });
  const data = query.data;
  return (
    <Card title="Holdings and allocation" actions={<Link to="/investments">Investments</Link>}>
      <QueryState loading={query.isPending} error={query.error} label="holdings">
        {data === undefined ? null : data.holdings.length === 0 ? (
          <p className="muted">No holdings yet.</p>
        ) : (
          <>
            <dl className="dashboard-figures">
              <div>
                <dt>Market value</dt>
                <dd><MoneyText amount={data.market_value.amount} currency={data.base_currency} tone="none" /></dd>
              </div>
              <div>
                <dt>Unrealised gain</dt>
                <dd><GainText gain={data.unrealized.total} cost={data.cost_basis} /></dd>
              </div>
            </dl>
            <AllocationBars title="Allocation by asset class" slices={data.allocation["asset_class"] ?? []} labels={titleCase} />
          </>
        )}
      </QueryState>
    </Card>
  );
}

export function LoansCard() {
  const query = useQuery({ queryKey: loanKeys.list, queryFn: listLoans });
  const loans = query.data ?? [];
  return (
    <Card title="Loans" actions={<Link to="/loans">All loans</Link>}>
      <QueryState loading={query.isPending} error={query.error} label="loans">
        {loans.length === 0 ? (
          <p className="muted">No loans yet.</p>
        ) : (
          <ul className="planning-list" aria-label="Loans">
            {loans.slice(0, 5).map((loan) => (
              <li key={loan.id}>
                <Link to="/loans/$loanId" params={{ loanId: loan.id }}>
                  {loan.lender ?? "Loan"}
                </Link>
                <span className="planning-list__item">
                  <MoneyText amount={loan.projection.balance.amount} currency={loan.projection.balance.currency} tone="none" />
                  <span className="muted">
                    {loan.projection.next_due_date === null ? "Paid off" : `next payment ${formatDate(loan.projection.next_due_date, { style: "short" }) ?? loan.projection.next_due_date}`}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        )}
      </QueryState>
    </Card>
  );
}

/** The widget for a catalogue id. */
export function DashboardWidget({ id }: { id: WidgetId }) {
  const { from, to } = monthRange(todayIsoDate());
  switch (id) {
    case "net_worth":
      return <NetWorthCard />;
    case "cash_flow":
      return <CashflowCard from={from} to={to} />;
    case "spending":
      return <SpendingCard from={from} to={to} />;
    case "budgets":
      return <BudgetsCard />;
    case "goals":
      return <GoalsCard />;
    case "recent_transactions":
      return <RecentCard />;
    case "review_queue":
      return <ReviewCard />;
    case "upcoming_bills":
      return <UpcomingBillsCard />;
    case "holdings":
      return <HoldingsCard />;
    case "loans":
      return <LoansCard />;
    case "tax_documents":
      return <TaxCard />;
    case "accounts":
      return <AccountsCard />;
  }
}
