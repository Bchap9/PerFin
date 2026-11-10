// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Trends: one month against the month before (month on month) and the same
 * month a year earlier (year on year), for spending or income, in total and
 * per category, with the monthly series that leads up to it.
 */
import type { UseQueryResult } from "@tanstack/react-query";

import { Card } from "../../components/Card";
import { EmptyState } from "../../components/EmptyState";
import { Field } from "../../components/Field";
import { MoneyText } from "../../components/MoneyText";
import { Select } from "../../components/Select";
import { Table, type TableColumn } from "../../components/Table";
import { sumAmounts } from "../../lib/money";
import type { Flow, TrendReport, TrendRow } from "./api";
import { allZero, flowName, itemLabel, monthLabel, trendBars, type MonthDatum } from "./chartData";
import { csvFilename, seriesCsv, trendCsv } from "./csv";
import { ChangeText, DownloadCsvButton, ReportQuery, UnconvertedNote } from "./parts";
import { shiftMonth } from "./periods";
import { ChartLegend, MonthColumnChart } from "./ReportCharts";
import { DEPTH_OPTIONS, TREND_MONTH_OPTIONS } from "./searchState";

const FLOW_OPTIONS: readonly { value: Flow; label: string }[] = [
  { value: "spending", label: "Spending" },
  { value: "income", label: "Income" },
];

function comparisonColumns(report: TrendReport): TableColumn<TrendRow>[] {
  const current = monthLabel(report.month, "short");
  const previous = monthLabel(shiftMonth(report.month, -1), "short");
  const yearAgo = monthLabel(shiftMonth(report.month, -12), "short");
  return [
    { id: "name", header: "Category", rowHeader: true, cell: (row) => itemLabel(row, "category") },
    { id: "amount", header: current, align: "end", cell: (row) => <MoneyText amount={row.amount.amount} currency={row.amount.currency} tone="none" /> },
    { id: "previous", header: previous, align: "end", cell: (row) => <MoneyText amount={row.previous.amount} currency={row.previous.currency} tone="none" /> },
    { id: "mom", header: "Month on month", align: "end", cell: (row) => <ChangeText change={row.mom_change} percent={row.mom_percent} /> },
    { id: "year-ago", header: yearAgo, align: "end", cell: (row) => <MoneyText amount={row.year_ago.amount} currency={row.year_ago.currency} tone="none" /> },
    { id: "yoy", header: "Year on year", align: "end", cell: (row) => <ChangeText change={row.yoy_change} percent={row.yoy_percent} /> },
  ];
}

function TrendReportView({ report, currentMonth }: { report: TrendReport; currentMonth: string }) {
  const name = flowName(report.flow);
  const lower = name.toLowerCase();
  const month = monthLabel(report.month);
  const previous = monthLabel(shiftMonth(report.month, -1));
  const yearAgo = monthLabel(shiftMonth(report.month, -12));
  const total = report.total;
  const bars = trendBars(report);
  const nothing = allZero(report.series.map((point) => point.amount.amount)) && report.categories.length === 0;

  if (nothing && report.unconverted.length === 0) {
    return (
      <EmptyState
        title={`No ${lower} to compare`}
        description={
          report.include_pending
            ? `Nothing counts as ${lower} in ${month} or the months before it in the accounts shown.`
            : `Nothing counts as ${lower} in ${month} or the months before it in the accounts shown. Transactions still waiting in Review are left out unless you include them above.`
        }
      />
    );
  }

  const columns = comparisonColumns(report);
  const seriesColumns: TableColumn<MonthDatum>[] = [
    { id: "month", header: "Month", rowHeader: true, cell: (bar) => bar.label },
    { id: "amount", header: name, align: "end", cell: (bar) => <MoneyText amount={bar.amount} currency={report.currency} tone="none" /> },
  ];

  return (
    <>
      <UnconvertedNote amounts={report.unconverted} currency={report.currency} what={`of ${lower} over the months compared and charted`} />
      <p className="report-summary">
        {month} against the month before and the same month a year earlier. Trends always use whole months
        {report.month === currentMonth ? `, and ${month} is not over yet, so its figures can still grow.` : "."}
      </p>
      <dl className="report-figures">
        <div>
          <dt>
            {name} in {month}
          </dt>
          <dd>
            <MoneyText amount={total.amount.amount} currency={total.amount.currency} tone="none" />
          </dd>
        </div>
        <div>
          <dt>Change from {previous}</dt>
          <dd>
            <ChangeText change={total.mom_change} percent={total.mom_percent} zeroBase={`Nothing in ${previous} to compare with`} />
          </dd>
        </div>
        <div>
          <dt>Change from {yearAgo}</dt>
          <dd>
            <ChangeText change={total.yoy_change} percent={total.yoy_percent} zeroBase={`Nothing in ${yearAgo} to compare with`} />
          </dd>
        </div>
      </dl>
      <Card
        title={`${name} month by month`}
        actions={
          <DownloadCsvButton
            label="Download CSV of the monthly totals"
            table={() => seriesCsv(report)}
            filename={csvFilename([lower, "by month", report.series[0]?.month ?? report.month, "to", report.month])}
          />
        }
      >
        <div className="report-section">
          <ChartLegend
            items={[
              { kind: "series-1", label: "Months compared" },
              { kind: "muted", label: "Other months" },
            ]}
          />
          <MonthColumnChart bars={bars} currency={report.currency} label={`${name} month by month`} valueLabel={name} compare />
          <details className="report-details">
            <summary>Monthly totals as a table</summary>
            <Table
              className="report-table"
              caption={`${name} month by month`}
              captionHidden
              columns={seriesColumns}
              rows={bars}
              rowKey={(bar) => bar.month}
              maxHeight="none"
              density="compact"
              footer={
                <tr>
                  <th scope="row">Total</th>
                  <td className="align-end" data-label={name}>
                    <MoneyText amount={sumAmounts(bars.map((bar) => bar.amount))} currency={report.currency} tone="none" />
                  </td>
                </tr>
              }
            />
          </details>
        </div>
      </Card>
      <Card
        title={`${month} by category`}
        actions={
          <DownloadCsvButton
            label="Download CSV of the comparison by category"
            table={() => trendCsv(report)}
            filename={csvFilename([lower, "trends", report.month])}
          />
        }
      >
        <div className="report-section">
          <Table
            className="report-table"
            caption={`${name} in ${month} by category, compared with ${previous} and ${yearAgo}`}
            captionHidden
            columns={columns}
            rows={report.categories}
            rowKey={(row) => row.key ?? "none"}
            maxHeight="none"
            empty={`No ${lower} in ${month}, ${previous} or ${yearAgo}.`}
            footer={
              <tr>
                <th scope="row">Total</th>
                <td className="align-end" data-label={monthLabel(report.month, "short")}>
                  <MoneyText amount={total.amount.amount} currency={total.amount.currency} tone="none" />
                </td>
                <td className="align-end" data-label={monthLabel(shiftMonth(report.month, -1), "short")}>
                  <MoneyText amount={total.previous.amount} currency={total.previous.currency} tone="none" />
                </td>
                <td className="align-end" data-label="Month on month">
                  <ChangeText change={total.mom_change} percent={total.mom_percent} />
                </td>
                <td className="align-end" data-label={monthLabel(shiftMonth(report.month, -12), "short")}>
                  <MoneyText amount={total.year_ago.amount} currency={total.year_ago.currency} tone="none" />
                </td>
                <td className="align-end" data-label="Year on year">
                  <ChangeText change={total.yoy_change} percent={total.yoy_percent} />
                </td>
              </tr>
            }
          />
          <p className="report-hint">
            Month on month is the change from {previous}; year on year is the change from {yearAgo}. A percentage is not shown when the earlier month was zero.
          </p>
        </div>
      </Card>
    </>
  );
}

export function TrendsView({
  query,
  flow,
  depth,
  months,
  currentMonth,
  onChange,
}: {
  query: UseQueryResult<TrendReport>;
  flow: Flow;
  depth: number;
  months: number;
  /** `YYYY-MM` of today, to say when the month compared is still running. */
  currentMonth: string;
  onChange: (patch: { flow?: Flow; depth?: number; months?: number }) => void;
}) {
  return (
    <div className="report-section">
      <div className="report-options" role="group" aria-label="View options">
        <Field label="Compare">
          <Select
            value={flow}
            options={FLOW_OPTIONS}
            onChange={(event) => {
              onChange({ flow: event.target.value === "income" ? "income" : "spending" });
            }}
          />
        </Field>
        <Field label="Category detail">
          <Select
            value={String(depth)}
            options={DEPTH_OPTIONS.map((option) => ({ value: String(option.value), label: option.label }))}
            onChange={(event) => {
              onChange({ depth: Number(event.target.value) });
            }}
          />
        </Field>
        <Field label="Months in the chart">
          <Select
            value={String(months)}
            options={TREND_MONTH_OPTIONS.map((value) => ({ value: String(value), label: `${String(value)} months` }))}
            onChange={(event) => {
              onChange({ months: Number(event.target.value) });
            }}
          />
        </Field>
      </div>
      <ReportQuery query={query} label="the report">
        {(report) => <TrendReportView report={report} currentMonth={currentMonth} />}
      </ReportQuery>
    </div>
  );
}
