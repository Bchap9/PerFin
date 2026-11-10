// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * CSV files of the report tables, built in the browser from the report
 * already on screen.
 *
 * Names in a report (payees above all) come from bank files, and a
 * spreadsheet runs a cell that starts with `=`, `+`, `-`, `@`, a tab or a
 * carriage return as a formula. Such cells get a leading apostrophe, which
 * spreadsheets show as text. A plain number ("-12.50") is left alone so
 * negative amounts stay numbers.
 */
import { isDecimalString } from "../../lib/money";
import { saveBlob } from "../settings/saveFile";
import type { CashflowReport, FlowReport, GroupBy, Money, TrendReport, TrendRow } from "./api";
import { itemLabel, sharePercent } from "./chartData";
import { shiftMonth } from "./periods";

export interface CsvTable {
  header: readonly string[];
  rows: readonly (readonly string[])[];
}

const FORMULA_START = /^[=+\-@\t\r]/;
const NEEDS_QUOTES = /[",\r\n]/;

/** "12", "-12.50": digits with an optional minus sign and fraction, nothing a spreadsheet could run. */
function isPlainNumber(value: string): boolean {
  return isDecimalString(value) && !value.startsWith("+");
}

/** One CSV field: formula characters neutralised, then quoted when needed (RFC 4180). */
export function csvCell(value: string): string {
  // Leading spaces do not make a formula safe: some importers trim them.
  const risky = !isPlainNumber(value) && (FORMULA_START.test(value) || FORMULA_START.test(value.trimStart()));
  const text = risky ? `'${value}` : value;
  return NEEDS_QUOTES.test(text) || text !== text.trim() ? `"${text.replace(/"/g, '""')}"` : text;
}

/** The whole file: one line per row, CRLF line ends. */
export function toCsv(table: CsvTable): string {
  return [table.header, ...table.rows].map((row) => `${row.map(csvCell).join(",")}\r\n`).join("");
}

const GROUP_HEADERS: ReadonlyMap<GroupBy, string> = new Map([
  ["category", "Category"],
  ["payee", "Payee"],
  ["month", "Month"],
  ["account", "Account"],
]);

export function groupHeader(groupBy: GroupBy): string {
  return GROUP_HEADERS.get(groupBy) ?? "Group";
}

const NOT_CONVERTED = "Not converted (no exchange rate)";

/** Rows for amounts the server could not convert: they are not part of the totals. */
function unconvertedRows(unconverted: readonly Money[], width: number, amountAt: number, currencyAt: number): string[][] {
  return unconverted.map((money) => {
    const row = Array.from({ length: width }, () => "");
    row.splice(0, 1, NOT_CONVERTED);
    row.splice(amountAt, 1, money.amount);
    row.splice(currencyAt, 1, money.currency);
    return row;
  });
}

/** Spending or income: one row per group, the total, then anything not converted. */
export function flowCsv(report: FlowReport): CsvTable {
  const total = report.total.amount;
  const rows = report.items.map((item) => [
    report.group_by === "month" ? (item.key ?? "") : itemLabel(item, report.group_by),
    item.amount.amount,
    item.amount.currency,
    sharePercent(item.amount.amount, total) ?? "",
  ]);
  return {
    header: [groupHeader(report.group_by), "Amount", "Currency", "Share of total (%)"],
    rows: [...rows, ["Total", total, report.total.currency, sharePercent(total, total) ?? ""], ...unconvertedRows(report.unconverted, 4, 1, 2)],
  };
}

/** Cash flow: one row per month, the totals, then anything not converted (a net amount). */
export function cashflowCsv(report: CashflowReport): CsvTable {
  return {
    header: ["Month", "Money in", "Money out", "Net", "Currency"],
    rows: [
      ...report.months.map((entry) => [entry.month, entry.inflow.amount, entry.outflow.amount, entry.net.amount, report.currency]),
      ["Total", report.inflow.amount, report.outflow.amount, report.net.amount, report.currency],
      ...unconvertedRows(report.unconverted, 5, 3, 4),
    ],
  };
}

function trendCsvRow(label: string, row: TrendRow): string[] {
  return [
    label,
    row.amount.amount,
    row.previous.amount,
    row.mom_change.amount,
    row.mom_percent ?? "",
    row.year_ago.amount,
    row.yoy_change.amount,
    row.yoy_percent ?? "",
    row.amount.currency,
  ];
}

/** Trends by category: the month, the month before and the same month a year earlier, with the changes. */
export function trendCsv(report: TrendReport): CsvTable {
  const previous = shiftMonth(report.month, -1);
  const yearAgo = shiftMonth(report.month, -12);
  return {
    header: [
      "Category",
      report.month,
      previous,
      `Change from ${previous}`,
      `Change from ${previous} (%)`,
      yearAgo,
      `Change from ${yearAgo}`,
      `Change from ${yearAgo} (%)`,
      "Currency",
    ],
    rows: [
      ...report.categories.map((row) => trendCsvRow(itemLabel(row, "category"), row)),
      trendCsvRow("Total", report.total),
      // The server reports these for all the months it read, not per month, so they fit no column.
      ...report.unconverted.map((money) => [`${NOT_CONVERTED}: ${money.amount} ${money.currency} over the months compared and charted`, "", "", "", "", "", "", "", ""]),
    ],
  };
}

/** The monthly series of a trend report. */
export function seriesCsv(report: TrendReport): CsvTable {
  return {
    header: ["Month", report.flow === "spending" ? "Spending" : "Income", "Currency"],
    rows: report.series.map((point) => [point.month, point.amount.amount, point.amount.currency]),
  };
}

/** "perfin-spending-by-category-2026-11-01-to-2026-11-30.csv": lower-case words joined by hyphens. */
export function csvFilename(parts: readonly string[]): string {
  const slug = parts
    .map((part) =>
      part
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-|-$/g, ""),
    )
    .filter((part) => part !== "")
    .join("-");
  return `perfin-${slug}.csv`;
}

/** Hand the table to the browser as a UTF-8 CSV file (with a byte-order mark so spreadsheets read names correctly). */
export function downloadCsv(table: CsvTable, filename: string): void {
  saveBlob(new Blob(["\uFEFF", toCsv(table)], { type: "text/csv;charset=utf-8" }), filename);
}
