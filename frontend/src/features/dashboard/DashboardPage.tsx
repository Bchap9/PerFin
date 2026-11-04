// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The home page: this month's cash flow, spending by category, budget
 * status, recent transactions, items waiting for review and the accounts.
 * Every card loads on its own so one failure does not blank the page.
 */
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useMemo } from "react";

import { Badge } from "../../components/Badge";
import { Card } from "../../components/Card";
import { EmptyState } from "../../components/EmptyState";
import { MoneyText } from "../../components/MoneyText";
import { Table, type TableColumn } from "../../components/Table";
import { endOfMonth, formatDate, formatMonth, startOfMonth, todayIsoDate } from "../../lib/dates";
import { sumAmounts } from "../../lib/money";
import { budgetKeys, getBudgetReport, listBudgets } from "../budgets/api";
import { barWidth, lineStatus } from "../budgets/report";
import { PlanningPage, QueryState } from "../planning/PlanningPage";
import { nameLookup, useAccounts, useCategories, type Account } from "../planning/queries";
import { listTaxYears, openMissing, reminderText } from "../tax/api";
import { dashboardKeys, getCashflow, getOpenReviewCount, getRecentTransactions, getSpendingByCategory } from "./api";

const MAX_BUDGETS = 3;

export function monthRange(today: string): { from: string; to: string } {
  return { from: startOfMonth(today), to: endOfMonth(today) };
}

/** Totals of open accounts per currency, assets and liabilities apart. */
export function accountTotals(accounts: readonly Account[]): { currency: string; assets: string; liabilities: string }[] {
  const byCurrency = new Map<string, { assets: string[]; liabilities: string[] }>();
  for (const account of accounts) {
    if (account.is_closed || !account.include_in_net_worth) {
      continue;
    }
    const entry = byCurrency.get(account.balance.currency) ?? { assets: [], liabilities: [] };
    (account.is_asset ? entry.assets : entry.liabilities).push(account.balance.amount);
    byCurrency.set(account.balance.currency, entry);
  }
  return [...byCurrency.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([currency, entry]) => ({ currency, assets: sumAmounts(entry.assets), liabilities: sumAmounts(entry.liabilities) }));
}

function CashflowCard({ from, to }: { from: string; to: string }) {
  const query = useQuery({ queryKey: dashboardKeys.cashflow(from, to), queryFn: () => getCashflow(from, to) });
  const report = query.data;
  return (
    <Card title={`Cash flow, ${formatMonth(from, { style: "long" }) ?? from}`}>
      <QueryState loading={query.isPending} error={query.error} label="cash flow">
        {report === undefined ? null : (
          <dl className="dashboard-figures">
            <div>
              <dt>Income</dt>
              <dd><MoneyText amount={report.inflow.amount} currency={report.currency} tone="signed" /></dd>
            </div>
            <div>
              <dt>Spending</dt>
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

function SpendingCard({ from, to }: { from: string; to: string }) {
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

function BudgetsCard() {
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

function RecentCard() {
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

function ReviewCard() {
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

function TaxCard() {
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

function AccountsCard() {
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

export function DashboardPage() {
  const { from, to } = monthRange(todayIsoDate());
  return (
    <PlanningPage title="Dashboard">
      <div className="planning-grid">
        <CashflowCard from={from} to={to} />
        <ReviewCard />
        <TaxCard />
        <SpendingCard from={from} to={to} />
        <BudgetsCard />
        <AccountsCard />
      </div>
      <RecentCard />
    </PlanningPage>
  );
}
