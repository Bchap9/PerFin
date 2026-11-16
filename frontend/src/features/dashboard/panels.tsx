// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The panels of Home (design note, stage 3, section 3). Each loads on its
 * own, shows its own spinner, and on a failure says in one line what failed,
 * with Retry, so one failing call never hides the rest of the page.
 */
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";

import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Chip } from "../../components/Chip";
import { MoneyText } from "../../components/MoneyText";
import { QuietNote } from "../../components/QuietNote";
import { Sparkline } from "../../components/Sparkline";
import { Spinner } from "../../components/Spinner";
import { StackBar } from "../../components/StackBar";
import { Stat, StatGroup } from "../../components/Stat";
import { errorMessage } from "../../lib/api/client";
import { categoryColourClass } from "../../lib/categoryColour";
import { formatDate } from "../../lib/dates";
import { amountSign, formatMoney, isZeroAmount, negateAmount } from "../../lib/money";
import { useInboxSummary } from "../../app/inboxSummary";
import { getLatest, netWorthKeys } from "../networth/api";
import { nameLookup, useCategories } from "../planning/queries";
import { listReviewCards, reviewKeys } from "../review/api";
import { merchantName, type Transaction } from "../transactions/api";
import {
  getComingUp,
  getNetWorthSeries,
  getRecentTransactions,
  getSeriesNeedingYou,
  getSpendingComparison,
  homeKeys,
} from "./api";
import { monthName } from "./helpers";
import {
  categorySourceText,
  comingUp,
  comparison,
  netWorthChange,
  needsYouRows,
  needsYouSeriesRows,
  OTHER_KEY,
  spendingPeriods,
  topCategories,
} from "./home";

const NEEDS_YOU_ROWS = 3;
const LATEST_ROWS = 5;

/** Loading and failure of a panel; `children` once its data is in. */
function PanelState({
  pending,
  error,
  label,
  onRetry,
  children,
}: {
  pending: boolean;
  error: unknown;
  label: string;
  onRetry: () => void;
  children: ReactNode;
}) {
  if (error !== null && error !== undefined) {
    return (
      <p role="alert" className="home-error">
        <span>
          Could not load {label}. {errorMessage(error)}
        </span>{" "}
        <Button size="sm" onClick={onRetry}>
          Retry
        </Button>
      </p>
    );
  }
  if (pending) {
    return <Spinner label={`Loading ${label}`} />;
  }
  return <>{children}</>;
}

function money(amount: string, currency: string, fractionDigits?: number): string {
  return formatMoney(amount, currency, fractionDigits === undefined ? {} : { fractionDigits }) ?? `${amount} ${currency}`;
}

function PanelLink({ to, children }: { to: string; children: ReactNode }) {
  return (
    <Link to={to} className="link-action">
      {children}
    </Link>
  );
}

// ---- Net worth ----------------------------------------------------------------

export function NetWorthPanel({ today }: { today: string }) {
  const latest = useQuery({ queryKey: netWorthKeys.latest, queryFn: getLatest });
  const series = useQuery({ queryKey: homeKeys.netWorthSeries, queryFn: getNetWorthSeries });
  const data = latest.data;
  const points = series.data?.points ?? [];
  const change = data === undefined ? null : netWorthChange(data.net.amount, points, today);
  const first = points.at(0);
  const last = points.at(-1);
  return (
    <Card title="Net worth" className="home-panel" actions={<PanelLink to="/accounts">Accounts</PanelLink>}>
      <PanelState
        pending={latest.isPending}
        error={latest.error}
        label="net worth"
        onRetry={() => {
          void latest.refetch();
          void series.refetch();
        }}
      >
        {data === undefined ? null : (
          <>
            <StatGroup>
              <Stat
                label="Today"
                size="display"
                value={<MoneyText amount={data.net.amount} currency={data.currency} tone="none" />}
                {...(change === null
                  ? {}
                  : {
                      change:
                        amountSign(change) === 0
                          ? { text: "No change this month", tone: "neutral" as const }
                          : {
                              text: `${amountSign(change) > 0 ? "Up" : "Down"} ${money(amountSign(change) > 0 ? change : negateAmount(change), data.currency)} this month`,
                              tone: amountSign(change) > 0 ? ("up" as const) : ("neutral" as const),
                            },
                    })}
              />
            </StatGroup>
            {data.unconverted.length === 0 ? null : (
              <QuietNote>Leaves out {data.unconverted.join(", ")}, which have no exchange rate</QuietNote>
            )}
          </>
        )}
      </PanelState>
      {series.isError ? (
        <p className="home-note">The 12-month line could not be loaded.</p>
      ) : first === undefined || last === undefined ? null : (
        <Sparkline
          className="home-spark"
          points={points.map((point) => Number(point.net.amount))}
          label={`Net worth over the last 12 months, from ${money(first.net.amount, first.net.currency)} to ${money(last.net.amount, last.net.currency)}`}
        />
      )}
    </Card>
  );
}

// ---- Needs you ----------------------------------------------------------------

function plural(count: number, one: string, many: string): string {
  return `${String(count)} ${count === 1 ? one : many}`;
}

export function NeedsYouPanel() {
  const summary = useInboxSummary();
  const cards = useQuery({ queryKey: reviewKeys.cards(), queryFn: () => listReviewCards() });
  const counts = summary.data;
  const documentRows = cards.data === undefined ? [] : needsYouRows(cards.data.cards, cards.data.loose);
  // The subscription cards come after every document and loose item, so they are asked for only when there is room.
  const seriesWanted = counts !== undefined && counts.subscriptions > 0 && cards.isSuccess && documentRows.length < NEEDS_YOU_ROWS;
  const series = useQuery({ queryKey: homeKeys.seriesNeedingYou, queryFn: getSeriesNeedingYou, enabled: seriesWanted });
  const seriesRows = seriesWanted && series.data !== undefined ? needsYouSeriesRows(series.data.suggested, series.data.events) : [];
  const rows = [...documentRows, ...seriesRows].slice(0, NEEDS_YOU_ROWS);
  const uncategorisedRow = counts !== undefined && (counts.uncategorised > 0 || counts.suggestions > 0);
  return (
    <Card title="Needs you" className="home-panel" actions={<PanelLink to="/inbox">Open Inbox</PanelLink>}>
      <PanelState
        pending={summary.isPending || cards.isPending || (seriesWanted && series.isPending)}
        error={summary.error ?? cards.error ?? (seriesWanted ? series.error : null)}
        label="what needs you"
        onRetry={() => {
          void summary.refetch();
          void cards.refetch();
          if (seriesWanted) {
            void series.refetch();
          }
        }}
      >
        {rows.length === 0 ? (
          <p className="home-empty">
            {uncategorisedRow ? "Every document is imported." : "Nothing is waiting. Every document is imported."}
          </p>
        ) : null}
        {rows.length === 0 && !uncategorisedRow ? null : (
          <ul className="home-rows" aria-label="What needs you">
            {rows.map((row) => (
              <li key={row.id}>
                <Link className="home-row" to="/inbox" search={row.document === null ? {} : { document: row.document }}>
                  <span className="home-row__main">
                    <span className="home-row__title">{row.title}</span>
                    <span className="home-row__sub">{row.question}</span>
                  </span>
                </Link>
              </li>
            ))}
            {uncategorisedRow ? (
              <li>
                <Link className="home-row" to="/transactions" search={{ category: "none" }}>
                  <span className="home-row__main">
                    <span className="home-row__title">
                      {plural(counts.uncategorised, "transaction has no category", "transactions have no category")}
                    </span>
                    {counts.suggestions > 0 ? (
                      <span className="home-row__sub">{plural(counts.suggestions, "has a suggestion", "have a suggestion")}</span>
                    ) : null}
                  </span>
                </Link>
              </li>
            ) : null}
          </ul>
        )}
      </PanelState>
    </Card>
  );
}

// ---- Spending this month ----------------------------------------------------------

export function SpendingPanel({ today }: { today: string }) {
  const periods = spendingPeriods(today);
  const query = useQuery({
    queryKey: homeKeys.spending(periods.from, periods.to, periods.previousFrom, periods.previousTo),
    queryFn: () => getSpendingComparison(periods.from, periods.to, periods.previousFrom, periods.previousTo),
  });
  const categories = useCategories();
  const tree = categories.data ?? [];
  const data = query.data;
  const previousMonth = monthName(periods.previousFrom);
  return (
    <Card title={`Spending in ${monthName(today)}`} className="home-panel" actions={<PanelLink to="/reports">Reports</PanelLink>}>
      <PanelState
        pending={query.isPending}
        error={query.error}
        label="spending"
        onRetry={() => {
          void query.refetch();
        }}
      >
        {data === undefined ? null : (
          <SpendingBody
            total={data.current.total.amount}
            previous={data.previous.total.amount}
            currency={data.current.currency}
            previousMonth={previousMonth}
            parts={topCategories(data.current.items).map((share) => ({
              label: share.label,
              value: Number(share.amount),
              colourClass: share.key === OTHER_KEY ? "chip--s6" : categoryColourClass(share.key, tree),
            }))}
          />
        )}
      </PanelState>
    </Card>
  );
}

function SpendingBody({
  total,
  previous,
  currency,
  previousMonth,
  parts,
}: {
  total: string;
  previous: string;
  currency: string;
  previousMonth: string;
  parts: { label: string; value: number; colourClass: string }[];
}) {
  const spent = amountSign(total) < 0 ? negateAmount(total) : total;
  return (
    <div className="home-stack">
      <StatGroup>
        <Stat
          label="Spent so far"
          value={<MoneyText amount={spent} currency={currency} tone="none" />}
          change={comparison(total, previous, previousMonth)}
        />
      </StatGroup>
      {parts.length === 0 || isZeroAmount(spent) ? (
        <p className="home-empty">No spending this month yet.</p>
      ) : (
        <StackBar
          label="Spending by category this month"
          parts={parts}
          format={(value) => money(String(value), currency)}
        />
      )}
    </div>
  );
}

// ---- Coming up ----------------------------------------------------------------

export function ComingUpPanel({ today }: { today: string }) {
  const query = useQuery({ queryKey: homeKeys.comingUp, queryFn: getComingUp });
  const items = query.data === undefined ? [] : comingUp(query.data.subscriptions, query.data.recurring, today);
  return (
    <Card title="Coming up" className="home-panel" actions={<PanelLink to="/plan/upcoming">Plan</PanelLink>}>
      <PanelState
        pending={query.isPending}
        error={query.error}
        label="what is coming up"
        onRetry={() => {
          void query.refetch();
        }}
      >
        {items.length === 0 ? (
          <p className="home-empty">Nothing is due in the next 30 days.</p>
        ) : (
          <ul className="home-rows" aria-label="Charges in the next 30 days">
            {items.map((item) => (
              <li key={item.id} className="home-row">
                <span className="home-row__main">
                  <span className="home-row__title">{item.name}</span>
                  <span className="home-row__sub">
                    {formatDate(item.date, { style: "medium" }) ?? item.date}
                    {item.note === null ? null : ` · ${item.note}`}
                  </span>
                </span>
                <span className="home-row__amount num">
                  {item.approximate ? (
                    <>
                      <span className="visually-hidden">about </span>
                      <span aria-hidden="true">~</span>
                      <MoneyText amount={item.amount} currency={item.currency} tone="none" fractionDigits={0} />
                    </>
                  ) : (
                    <MoneyText amount={item.amount} currency={item.currency} tone="none" />
                  )}
                </span>
              </li>
            ))}
          </ul>
        )}
      </PanelState>
    </Card>
  );
}

// ---- Latest transactions ----------------------------------------------------------

function CategoryCell({ row, name, tree }: { row: Transaction; name: (id: string | null) => string; tree: Parameters<typeof categoryColourClass>[1] }) {
  if (row.splits.length > 0) {
    return <Chip>Split</Chip>;
  }
  if (row.category_id !== null) {
    const source = categorySourceText(row.category_source);
    return (
      <>
        <Chip colourClass={categoryColourClass(row.category_id, tree)}>{name(row.category_id)}</Chip>
        {source === null ? null : <span className="home-row__source">{source}</span>}
      </>
    );
  }
  const suggested = row.suggested_category_id ?? null;
  if (suggested !== null) {
    return (
      <>
        <Chip tone="info">{`${name(suggested)}?`}</Chip>
        <span className="home-row__source">Suggestion</span>
      </>
    );
  }
  return <Chip colourClass="chip--none">No category</Chip>;
}

export function LatestPanel() {
  const query = useQuery({ queryKey: homeKeys.latest, queryFn: () => getRecentTransactions(LATEST_ROWS) });
  const categories = useCategories();
  const tree = categories.data ?? [];
  const name = nameLookup(categories.data);
  const rows = (query.data?.items ?? []).slice(0, LATEST_ROWS);
  return (
    <Card
      title="Latest transactions"
      className="home-panel home-panel--wide"
      actions={<PanelLink to="/transactions">All transactions</PanelLink>}
    >
      <PanelState
        pending={query.isPending}
        error={query.error}
        label="the latest transactions"
        onRetry={() => {
          void query.refetch();
        }}
      >
        {rows.length === 0 ? (
          <p className="home-empty">No transactions yet.</p>
        ) : (
          <ul className="home-rows" aria-label="The five latest transactions">
            {rows.map((row) => {
              const merchant = merchantName(row);
              const descriptor = row.raw_payee ?? row.memo;
              const title = merchant ?? descriptor ?? "Transaction";
              const date = formatDate(row.posted_date, { style: "medium" }) ?? row.posted_date;
              return (
                <li key={row.id}>
                  <Link className="home-row home-row--tx" to="/transactions" search={{ tx: row.id }}>
                    <span className="home-row__main">
                      <span className="home-row__title">{title}</span>
                      <span className="home-row__sub">
                        {date}
                        {merchant !== null && descriptor !== null && descriptor !== merchant ? ` · ${descriptor}` : null}
                      </span>
                    </span>
                    <span className="home-row__category">
                      <CategoryCell row={row} name={name} tree={tree} />
                    </span>
                    <span className="home-row__amount num">
                      <MoneyText amount={row.amount.amount} currency={row.amount.currency} tone="signed" signDisplay="exceptZero" />
                    </span>
                  </Link>
                </li>
              );
            })}
          </ul>
        )}
      </PanelState>
    </Card>
  );
}
