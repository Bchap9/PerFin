// SPDX-License-Identifier: AGPL-3.0-or-later
/** Cash flow for the period: money in, money out and net per month, with the totals. */
import type { UseQueryResult } from "@tanstack/react-query";

import { Card } from "../../components/Card";
import { EmptyState } from "../../components/EmptyState";
import { MoneyText } from "../../components/MoneyText";
import { Table, type TableColumn } from "../../components/Table";
import type { CashflowMonth, CashflowReport } from "./api";
import { allZero, cashflowBars, monthLabel } from "./chartData";
import { cashflowCsv, csvFilename } from "./csv";
import { DownloadCsvButton, ReportQuery, UnconvertedNote } from "./parts";
import { describeRange } from "./periods";
import { CashflowChart, ChartLegend } from "./ReportCharts";

const columns: TableColumn<CashflowMonth>[] = [
  { id: "month", header: "Month", rowHeader: true, cell: (entry) => monthLabel(entry.month) },
  { id: "in", header: "Money in", align: "end", cell: (entry) => <MoneyText amount={entry.inflow.amount} currency={entry.inflow.currency} tone="none" /> },
  { id: "out", header: "Money out", align: "end", cell: (entry) => <MoneyText amount={entry.outflow.amount} currency={entry.outflow.currency} tone="none" /> },
  { id: "net", header: "Net", align: "end", cell: (entry) => <MoneyText amount={entry.net.amount} currency={entry.net.currency} tone="signed" signDisplay="exceptZero" /> },
];

function CashflowReportView({ report }: { report: CashflowReport }) {
  const range = describeRange({ from: report.date_from, to: report.date_to });
  const nothing = allZero(report.months.flatMap((entry) => [entry.inflow.amount, entry.outflow.amount]));

  if (nothing && report.unconverted.length === 0) {
    return (
      <EmptyState
        title="No money in or out in this period"
        description={
          report.include_pending
            ? `Nothing was posted from ${range} in the accounts shown.`
            : `Nothing was posted from ${range} in the accounts shown. Transactions still waiting in Review are left out unless you include them above.`
        }
      />
    );
  }

  return (
    <>
      <UnconvertedNote amounts={report.unconverted} currency={report.currency} what="of net cash flow (a minus sign means money out)" />
      <dl className="report-figures">
        <div>
          <dt>Money in</dt>
          <dd>
            <MoneyText amount={report.inflow.amount} currency={report.inflow.currency} tone="none" />
          </dd>
        </div>
        <div>
          <dt>Money out</dt>
          <dd>
            <MoneyText amount={report.outflow.amount} currency={report.outflow.currency} tone="none" />
          </dd>
        </div>
        <div>
          <dt>Net</dt>
          <dd>
            <MoneyText amount={report.net.amount} currency={report.net.currency} tone="signed" signDisplay="exceptZero" />
          </dd>
        </div>
      </dl>
      <Card
        title="Cash flow by month"
        actions={<DownloadCsvButton table={() => cashflowCsv(report)} filename={csvFilename(["cash flow", report.date_from, "to", report.date_to])} />}
      >
        <div className="report-section">
          {nothing ? null : (
            <>
              <ChartLegend
                items={[
                  { kind: "series-1", label: "Money in" },
                  { kind: "series-2", label: "Money out" },
                  { kind: "net", label: "Net (in minus out)" },
                ]}
              />
              <CashflowChart bars={cashflowBars(report)} currency={report.currency} label="Money in, money out and net by month" />
            </>
          )}
          <Table
            className="report-table"
            caption={`Cash flow by month, ${range}`}
            captionHidden
            columns={columns}
            rows={report.months}
            rowKey={(entry) => entry.month}
            maxHeight="none"
            footer={
              <tr>
                <th scope="row">Total</th>
                <td className="align-end" data-label="Money in">
                  <MoneyText amount={report.inflow.amount} currency={report.inflow.currency} tone="none" />
                </td>
                <td className="align-end" data-label="Money out">
                  <MoneyText amount={report.outflow.amount} currency={report.outflow.currency} tone="none" />
                </td>
                <td className="align-end" data-label="Net">
                  <MoneyText amount={report.net.amount} currency={report.net.currency} tone="signed" signDisplay="exceptZero" />
                </td>
              </tr>
            }
          />
          <p className="report-hint">
            Money in is every amount received and money out every amount paid. A refund counts as money in here, while the Spending tab takes it off its category, so
            money in and money out can be higher than income and spending. Net is the same either way.
          </p>
        </div>
      </Card>
    </>
  );
}

export function CashflowView({ query }: { query: UseQueryResult<CashflowReport> }) {
  return (
    <ReportQuery query={query} label="the report">
      {(report) => <CashflowReportView report={report} />}
    </ReportQuery>
  );
}
