// SPDX-License-Identifier: AGPL-3.0-or-later
/** Pieces the report views share. */
import "./reports.css";

import type { UseQueryResult } from "@tanstack/react-query";
import { Fragment, type ReactNode } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { MoneyText } from "../../components/MoneyText";
import { QueryState } from "../planning/PlanningPage";
import type { Money } from "./api";
import { formatPercent } from "./chartData";
import { downloadCsv, type CsvTable } from "./csv";

/**
 * Loading, error and loaded states of one report. While a changed filter
 * loads, the previous report stays in place, dimmed, so the page does not
 * jump; `children` gets the report to show.
 */
export function ReportQuery<T>({ query, label, children }: { query: UseQueryResult<T>; label: string; children: (report: T) => ReactNode }) {
  const report = query.data;
  return (
    <QueryState loading={query.isPending} error={query.error} label={label}>
      {report === undefined ? null : (
        <div className={query.isPlaceholderData ? "report-view report-view--stale" : "report-view"} aria-busy={query.isPlaceholderData || undefined}>
          {children(report)}
        </div>
      )}
    </QueryState>
  );
}

/**
 * Amounts the server could not convert into the report currency. They are
 * not in any total, so they are always shown, each in its own currency.
 */
export function UnconvertedNote({ amounts, currency, what }: { amounts: readonly Money[]; currency: string; what: string }) {
  if (amounts.length === 0) {
    return null;
  }
  return (
    <p role="status" className="report-note">
      <Badge tone="warning">Not included</Badge>{" "}
      {amounts.map((money, index) => (
        <Fragment key={money.currency}>
          {index === 0 ? null : ", "}
          <MoneyText amount={money.amount} currency={money.currency} tone="none" />
        </Fragment>
      ))}{" "}
      {what} could not be converted to {currency} because there is no exchange rate for the day. {amounts.length === 1 ? "It is" : "They are"} left out of the totals,
      the chart and the table.
    </p>
  );
}

/** "Download CSV"; `label` names the table for assistive technology when a view has more than one. */
export function DownloadCsvButton({ table, filename, label }: { table: () => CsvTable; filename: string; label?: string }) {
  return (
    <Button
      size="sm"
      aria-label={label}
      onClick={() => {
        downloadCsv(table(), filename);
      }}
    >
      Download CSV
    </Button>
  );
}

/**
 * A change between two months: the amount with its sign and, below it, the
 * server's percentage. Not coloured: more spending and more income are not
 * the same kind of news. No percentage exists when the earlier month was zero.
 */
export function ChangeText({ change, percent, zeroBase }: { change: Money; percent: string | null; /** Shown in place of a dash where there is room for words. */ zeroBase?: string }) {
  return (
    <span className="report-change">
      <MoneyText amount={change.amount} currency={change.currency} tone="none" signDisplay="exceptZero" />
      <span className="report-change__percent">
        {percent !== null
          ? formatPercent(percent, { signed: true })
          : (zeroBase ?? (
              <>
                <span aria-hidden="true">—</span>
                <span className="visually-hidden">No percentage</span>
              </>
            ))}
      </span>
    </span>
  );
}
