// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Reports: spending, income, cash flow and trends over a chosen period.
 *
 * The page state (tab, period, account, grouping) lives in the address
 * (searchState.ts). One control bar scopes all four tabs; each tab loads its
 * report only while it is open. The server decides what counts (backend
 * `app.domain.reports`): transfers and ignored categories are left out,
 * pending review rows only count when asked, and amounts without an
 * exchange rate come back separately as `unconverted`; the views show all of
 * that as the server reports it.
 */
import "./reports.css";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useMemo } from "react";

import { EmptyState } from "../../components/EmptyState";
import { Tabs } from "../../components/Tabs";
import { todayIsoDate } from "../../lib/dates";
import { PlanningPage } from "../planning/PlanningPage";
import { useAccounts } from "../planning/queries";
import {
  getCashflowReport,
  getFlowReport,
  getTrendReport,
  reportKeys,
  type CashflowParams,
  type Flow,
  type FlowParams,
  type ReportScope,
  type TrendParams,
} from "./api";
import { CashflowView } from "./CashflowView";
import { accountLabel, ControlBar } from "./ControlBar";
import { FlowView } from "./FlowView";
import { describeRange, monthOf, rangeProblemText, resolvePeriod } from "./periods";
import { parseReportSearch, reportSearch, TAB_IDS, type ReportState } from "./searchState";
import { TrendsView } from "./TrendsView";

function HowItWorks({ currency }: { currency: string | undefined }) {
  const base = currency ?? "your household's main currency";
  return (
    <details className="report-details">
      <summary>How these figures are worked out</summary>
      <ul>
        <li>Dates are the dates transactions were posted. A period can cover at most ten years.</li>
        <li>
          Moving money between your own accounts is not income or spending: transfers are left out, and so is anything in a category marked as a transfer or as
          ignored.
        </li>
        <li>Transactions still waiting in Review are left out unless you tick the box above.</li>
        <li>A split transaction counts under the category of each of its parts.</li>
        <li>
          Spending is everything in a spending category plus uncategorised money out; a refund in a spending category lowers it. Income is everything in an income
          category plus uncategorised money in.
        </li>
        <li>
          Amounts are in {base}. An amount in another currency is converted at the exchange rate of the day it was posted. When there is no rate, the amount is not
          guessed: it is left out of the totals and listed separately in its own currency.
        </li>
        <li>Only accounts you can see are counted.</li>
      </ul>
    </details>
  );
}

export function ReportsPage() {
  const search: Record<string, unknown> = useSearch({ strict: false });
  const navigate = useNavigate();
  const accounts = useAccounts();
  const state = useMemo(() => parseReportSearch(search), [search]);
  const today = todayIsoDate();
  const resolved = resolvePeriod(state.period, { from: state.from, to: state.to }, today);
  const range = resolved.range;
  const ready = range !== null;
  const dates = range ?? { from: "", to: "" };
  const scope: ReportScope = { account: state.account, pending: state.pending };

  const flowParams = (flow: Flow): FlowParams => ({ flow, from: dates.from, to: dates.to, groupBy: state.group, depth: state.depth, ...scope });
  const spendingParams = flowParams("spending");
  const incomeParams = flowParams("income");
  const cashflowParams: CashflowParams = { from: dates.from, to: dates.to, ...scope };
  // Trends compare whole months: the month the period ends in, the one before and the one a year earlier.
  const trendParams: TrendParams = { flow: state.flow, month: monthOf(dates.to), months: state.months, depth: state.depth, ...scope };

  const spending = useQuery({
    queryKey: reportKeys.flow(spendingParams),
    queryFn: () => getFlowReport(spendingParams),
    enabled: ready && state.tab === "spending",
    placeholderData: keepPreviousData,
  });
  const income = useQuery({
    queryKey: reportKeys.flow(incomeParams),
    queryFn: () => getFlowReport(incomeParams),
    enabled: ready && state.tab === "income",
    placeholderData: keepPreviousData,
  });
  const cashflow = useQuery({
    queryKey: reportKeys.cashflow(cashflowParams),
    queryFn: () => getCashflowReport(cashflowParams),
    enabled: ready && state.tab === "cashflow",
    placeholderData: keepPreviousData,
  });
  const trends = useQuery({
    queryKey: reportKeys.trends(trendParams),
    queryFn: () => getTrendReport(trendParams),
    enabled: ready && state.tab === "trends",
    placeholderData: keepPreviousData,
  });
  const active = state.tab === "spending" ? spending.data : state.tab === "income" ? income.data : state.tab === "cashflow" ? cashflow.data : trends.data;

  const update = (patch: Partial<ReportState>): void => {
    // Built from the address as it is when the change lands, so two quick changes both count.
    // Replace, so Back leaves the page instead of undoing one filter at a time.
    void navigate({ to: "/reports", search: (current: Record<string, unknown>) => reportSearch({ ...parseReportSearch(current), ...patch }), replace: true });
  };

  // Until the box is ticked or cleared, the household setting decides; the report says which way it went.
  const pending = state.pending ?? active?.include_pending ?? false;
  const account = state.account === "" ? undefined : accounts.data?.find((entry) => entry.id === state.account);
  const needsDates = resolved.problem === null ? null : <EmptyState title="Choose the dates" description={rangeProblemText(resolved.problem)} />;

  return (
    <PlanningPage title="Reports">
      <div className="report-page planning-stack">
        <ControlBar
          values={{ period: state.period, from: state.from, to: state.to, account: state.account }}
          resolved={resolved}
          presetDates={range}
          accounts={accounts.data}
          pendingChecked={pending}
          onChange={update}
        />
        {range === null ? null : (
          <p className="report-summary">
            Showing {describeRange(range)} for {state.account === "" ? "all accounts" : account === undefined ? "one account" : accountLabel(account)}. Transactions
            awaiting review are {pending ? "included" : "left out"}.
          </p>
        )}
        <Tabs
          label="Reports"
          selectedId={state.tab}
          onSelect={(id) => {
            update({ tab: TAB_IDS.find((tab) => tab === id) ?? "spending" });
          }}
          tabs={[
            {
              id: "spending",
              label: "Spending",
              content: needsDates ?? <FlowView query={spending} group={state.group} depth={state.depth} account={state.account} onChange={update} />,
            },
            {
              id: "income",
              label: "Income",
              content: needsDates ?? <FlowView query={income} group={state.group} depth={state.depth} account={state.account} onChange={update} />,
            },
            { id: "cashflow", label: "Cash flow", content: needsDates ?? <CashflowView query={cashflow} /> },
            {
              id: "trends",
              label: "Trends",
              content: needsDates ?? (
                <TrendsView query={trends} flow={state.flow} depth={state.depth} months={state.months} currentMonth={monthOf(today)} onChange={update} />
              ),
            },
          ]}
        />
        <HowItWorks currency={active?.currency} />
      </div>
    </PlanningPage>
  );
}
