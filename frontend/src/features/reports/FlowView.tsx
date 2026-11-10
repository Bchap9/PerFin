// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Spending or income for the period, grouped by category, payee, month or
 * account: the total, a chart and a table with each group's share. Labels
 * are taken from the report itself (its grouping and dates), so what is on
 * screen always describes the figures it sits next to.
 */
import type { UseQueryResult } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import { Card } from "../../components/Card";
import { EmptyState } from "../../components/EmptyState";
import { Field } from "../../components/Field";
import { MoneyText } from "../../components/MoneyText";
import { Select } from "../../components/Select";
import { Table, type TableColumn } from "../../components/Table";
import { compareAmounts } from "../../lib/money";
import type { FlowReport, GroupBy, ReportItem } from "./api";
import { allZero, flowName, formatPercent, groupNoun, itemLabel, monthBars, rankedBars, sharePercent } from "./chartData";
import { csvFilename, flowCsv, groupHeader } from "./csv";
import { transactionsSearch } from "./links";
import { DownloadCsvButton, ReportQuery, UnconvertedNote } from "./parts";
import { describeRange } from "./periods";
import { MonthColumnChart, RankedBarChart } from "./ReportCharts";
import { DEPTH_OPTIONS, GROUP_OPTIONS } from "./searchState";

function largest(report: FlowReport): ReportItem | undefined {
  // Category, payee and account rows arrive largest first; months arrive in date order.
  return report.items.reduce<ReportItem | undefined>(
    (best, item) => (best === undefined || compareAmounts(item.amount.amount, best.amount.amount) > 0 ? item : best),
    undefined,
  );
}

function Share({ amount, total }: { amount: string; total: string }) {
  const share = sharePercent(amount, total);
  return share === null ? (
    <>
      <span aria-hidden="true">—</span>
      <span className="visually-hidden">No share: the total is zero</span>
    </>
  ) : (
    <>{formatPercent(share)}</>
  );
}

function FlowReportView({ report, account }: { report: FlowReport; account: string }) {
  const name = flowName(report.flow);
  const lower = name.toLowerCase();
  const group = report.group_by;
  const title = `${name} by ${groupNoun(group, 1)}`;
  const range = describeRange({ from: report.date_from, to: report.date_to });
  const nothing = allZero(report.items.map((item) => item.amount.amount));

  if (nothing && report.unconverted.length === 0) {
    return (
      <EmptyState
        title={`No ${lower} in this period`}
        description={
          report.include_pending
            ? `Nothing counts as ${lower} from ${range} in the accounts shown.`
            : `Nothing counts as ${lower} from ${range} in the accounts shown. Transactions still waiting in Review are left out unless you include them above.`
        }
      />
    );
  }

  const top = largest(report);
  const bars = group === "month" ? [] : rankedBars(report);
  const months = group === "month" ? monthBars(report) : [];
  const columns: TableColumn<ReportItem>[] = [
    {
      id: "name",
      header: groupHeader(group),
      rowHeader: true,
      cell: (item) => {
        const search = transactionsSearch(report, item, account);
        const label = itemLabel(item, group);
        return search === null ? (
          label
        ) : (
          <Link to="/transactions" search={search}>
            {label}
          </Link>
        );
      },
    },
    { id: "amount", header: "Amount", align: "end", cell: (item) => <MoneyText amount={item.amount.amount} currency={item.amount.currency} tone="none" /> },
    { id: "share", header: "Share of total", align: "end", cell: (item) => <Share amount={item.amount.amount} total={report.total.amount} /> },
  ];

  return (
    <>
      <UnconvertedNote amounts={report.unconverted} currency={report.currency} what={`of ${lower}`} />
      <dl className="report-figures">
        <div>
          <dt>Total {lower}</dt>
          <dd>
            <MoneyText amount={report.total.amount} currency={report.total.currency} tone="none" />
          </dd>
        </div>
        {top === undefined || nothing || report.items.length < 2 ? null : (
          <div>
            <dt>{group === "month" ? "Highest month" : `Largest ${groupNoun(group, 1)}`}</dt>
            <dd>
              <MoneyText amount={top.amount.amount} currency={top.amount.currency} tone="none" />
              <span className="report-figures__detail">{itemLabel(top, group)}</span>
            </dd>
          </div>
        )}
      </dl>
      <Card
        title={title}
        actions={<DownloadCsvButton table={() => flowCsv(report)} filename={csvFilename([lower, "by", groupNoun(group, 1), report.date_from, "to", report.date_to])} />}
      >
        <div className="report-section">
          {/* One bar is a number, not a chart: the figure above already says it. */}
          {bars.length >= 2 ? <RankedBarChart bars={bars} currency={report.currency} label={title} valueLabel={name} /> : null}
          {months.length >= 2 ? <MonthColumnChart bars={months} currency={report.currency} label={title} valueLabel={name} /> : null}
          <Table
            className="report-table"
            caption={`${title}, ${range}`}
            captionHidden
            columns={columns}
            rows={report.items}
            rowKey={(item) => item.key ?? "none"}
            maxHeight="none"
            empty={`No ${lower} was converted for this period.`}
            footer={
              <tr>
                <th scope="row">Total</th>
                <td className="align-end" data-label="Amount">
                  <MoneyText amount={report.total.amount} currency={report.total.currency} tone="none" />
                </td>
                <td className="align-end" data-label="Share of total">
                  <Share amount={report.total.amount} total={report.total.amount} />
                </td>
              </tr>
            }
          />
          {group === "category" ? <p className="report-hint">
              A category name opens its transactions for these dates. A category shown with its subcategories rolled in lists
              only its own; transfers and rows awaiting review, which reports leave out, are listed there too.
            </p> : null}
          {group === "account" ? <p className="report-hint">An account name opens that account&apos;s transactions.</p> : null}
        </div>
      </Card>
    </>
  );
}

export function FlowView({
  query,
  group,
  depth,
  account,
  onChange,
}: {
  query: UseQueryResult<FlowReport>;
  group: GroupBy;
  depth: number;
  account: string;
  onChange: (patch: { group?: GroupBy; depth?: number }) => void;
}) {
  return (
    <div className="report-section">
      <div className="report-options" role="group" aria-label="View options">
        <Field label="Group by">
          <Select
            value={group}
            options={GROUP_OPTIONS}
            onChange={(event) => {
              onChange({ group: GROUP_OPTIONS.find((option) => option.value === event.target.value)?.value ?? "category" });
            }}
          />
        </Field>
        {group === "category" ? (
          <Field label="Category detail">
            <Select
              value={String(depth)}
              options={DEPTH_OPTIONS.map((option) => ({ value: String(option.value), label: option.label }))}
              onChange={(event) => {
                onChange({ depth: Number(event.target.value) });
              }}
            />
          </Field>
        ) : null}
      </div>
      <ReportQuery query={query} label="the report">
        {(report) => <FlowReportView report={report} account={account} />}
      </ReportQuery>
    </div>
  );
}
