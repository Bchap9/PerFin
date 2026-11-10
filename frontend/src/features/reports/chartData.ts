// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Mapping report answers to chart data and labels. Exact amounts stay
 * decimal strings; the only numbers are the display-only `plot` values a
 * chart needs to place its marks.
 */
import { formatMonth } from "../../lib/dates";
import { decimalToString, formatDecimal, isZeroAmount, parseDecimal, roundDecimal, sumAmounts, tryParseDecimal } from "../../lib/money";
import type { CashflowReport, Flow, FlowReport, GroupBy, ReportItem, TrendReport } from "./api";
import { shiftMonth } from "./periods";

/** "November 2026" (long) or "Nov 2026" (short) for `YYYY-MM`; the input when it is not a month. */
export function monthLabel(month: string, style: "long" | "short" = "long"): string {
  return formatMonth(`${month}-01`, { style }) ?? month;
}

/** Axis label of a month: the year only on the first tick and each January ("Nov 2026", "Dec", "Jan 2027"). */
export function monthTick(month: string, first: string): string {
  if (month === first || month.endsWith("-01")) {
    return monthLabel(month, "short");
  }
  const short = monthLabel(month, "short");
  // "Nov 2026" without the year, whatever the locale's order.
  return short.replace(/\s*\d{4}\s*/u, "").trim() || short;
}

export function flowName(flow: Flow): string {
  return flow === "spending" ? "Spending" : "Income";
}

const GROUP_NOUNS: ReadonlyMap<GroupBy, { one: string; many: string }> = new Map([
  ["category", { one: "category", many: "categories" }],
  ["payee", { one: "payee", many: "payees" }],
  ["month", { one: "month", many: "months" }],
  ["account", { one: "account", many: "accounts" }],
]);

export function groupNoun(groupBy: GroupBy, count: number): string {
  const noun = GROUP_NOUNS.get(groupBy) ?? { one: "group", many: "groups" };
  return count === 1 ? noun.one : noun.many;
}

/** The name of a report row: the server's name, the month, or what a missing key means. */
export function itemLabel(item: Pick<ReportItem, "key" | "name">, groupBy: GroupBy): string {
  if (groupBy === "month") {
    return item.key === null ? "Unknown month" : monthLabel(item.key);
  }
  if (item.name !== null) {
    return item.name;
  }
  switch (groupBy) {
    case "category":
      return item.key === null ? "Uncategorised" : "Unknown category";
    case "payee":
      return item.key === null ? "No payee" : "Unknown payee";
    case "account":
      return "Unknown account";
  }
}

export interface BarDatum {
  key: string;
  label: string;
  /** Exact amount. */
  amount: string;
  /** Display-only. */
  plot: number;
}

export const REST_KEY = "__rest__";
export const DEFAULT_BAR_LIMIT = 12;

/**
 * Bars for a report grouped by category, payee or account, in the server's
 * order (largest first). Past `limit` bars the smallest are added up into
 * one "N smaller ..." bar so the chart stays readable; the table lists them all.
 */
export function rankedBars(report: Pick<FlowReport, "items" | "group_by">, limit = DEFAULT_BAR_LIMIT): BarDatum[] {
  const bars = report.items.map((item, index) => ({
    key: item.key ?? `none-${String(index)}`,
    label: itemLabel(item, report.group_by),
    amount: item.amount.amount,
    plot: Number(item.amount.amount),
  }));
  if (bars.length <= limit) {
    return bars;
  }
  const kept = bars.slice(0, limit - 1);
  const rest = bars.slice(limit - 1);
  const amount = sumAmounts(rest.map((bar) => bar.amount));
  return [...kept, { key: REST_KEY, label: `${String(rest.length)} smaller ${groupNoun(report.group_by, rest.length)}`, amount, plot: Number(amount) }];
}

export type MonthRole = "current" | "previous" | "year-ago" | "other";

export interface MonthDatum {
  /** `YYYY-MM`. */
  month: string;
  label: string;
  amount: string;
  plot: number;
  /** Trends only: whether this is one of the three months being compared. */
  role: MonthRole;
}

/** One column per month of a report grouped by month. */
export function monthBars(report: Pick<FlowReport, "items">): MonthDatum[] {
  return report.items.map((item) => ({
    month: item.key ?? "",
    label: item.key === null ? "Unknown month" : monthLabel(item.key),
    amount: item.amount.amount,
    plot: Number(item.amount.amount),
    role: "other",
  }));
}

/** The monthly series of a trend report, marking the month compared, the one before and the one a year earlier. */
export function trendBars(report: Pick<TrendReport, "series" | "month">): MonthDatum[] {
  const previous = shiftMonth(report.month, -1);
  const yearAgo = shiftMonth(report.month, -12);
  return report.series.map((point) => ({
    month: point.month,
    label: monthLabel(point.month),
    amount: point.amount.amount,
    plot: Number(point.amount.amount),
    role: point.month === report.month ? "current" : point.month === previous ? "previous" : point.month === yearAgo ? "year-ago" : "other",
  }));
}

export interface CashflowDatum {
  month: string;
  label: string;
  inflow: string;
  outflow: string;
  net: string;
  plotIn: number;
  plotOut: number;
  plotNet: number;
}

export function cashflowBars(report: Pick<CashflowReport, "months">): CashflowDatum[] {
  return report.months.map((entry) => ({
    month: entry.month,
    label: monthLabel(entry.month),
    inflow: entry.inflow.amount,
    outflow: entry.outflow.amount,
    net: entry.net.amount,
    plotIn: Number(entry.inflow.amount),
    plotOut: Number(entry.outflow.amount),
    plotNet: Number(entry.net.amount),
  }));
}

/** True when every amount is zero (or there are none): nothing to draw. */
export function allZero(amounts: readonly string[]): boolean {
  return amounts.every((amount) => isZeroAmount(amount));
}

/** `part / whole` as a percentage with one decimal ("62.4"); `null` when `whole` is zero. Integer arithmetic only. */
export function sharePercent(part: string, whole: string): string | null {
  const a = tryParseDecimal(part);
  const b = tryParseDecimal(whole);
  if (a === null || b === null || b.units === 0n) {
    return null;
  }
  // (a.units / 10^a.scale) / (b.units / 10^b.scale) * 100 with two decimals, then rounded to one.
  const numerator = a.units * 10n ** BigInt(b.scale) * 10000n;
  const denominator = b.units * 10n ** BigInt(a.scale);
  return decimalToString(roundDecimal({ units: numerator / denominator, scale: 2 }, 1));
}

/** "62.4%" for a percent amount; with `signed`, "+12.5%" and "-3.0%". */
export function formatPercent(percent: string, options: { signed?: boolean } = {}): string {
  const rounded = decimalToString(roundDecimal(parseDecimal(percent), 1));
  const text = formatDecimal(rounded, { minimumFractionDigits: 1, maximumFractionDigits: 1, signDisplay: options.signed === true ? "exceptZero" : "auto" });
  return `${text ?? rounded}%`;
}

export interface ValueAxis {
  /** Round tick values from the lowest to the highest; they always include zero. */
  ticks: number[];
  /** Whether every tick is a whole number (labels then need no decimals). */
  whole: boolean;
}

const NICE_STEPS: readonly number[] = [1, 2, 2.5, 5];

/**
 * A value axis with clean numbers (0 / 1,000 / 2,000 rather than 0 / 950 /
 * 1,900): the smallest step of 1, 2, 2.5 or 5 times a power of ten that
 * covers zero and every plotted value in at most `maxSteps` steps. Display
 * only, like the values plotted.
 */
export function valueAxis(values: readonly number[], maxSteps = 5): ValueAxis {
  const finite = values.filter((value) => Number.isFinite(value));
  const low = Math.min(0, ...finite);
  const high = Math.max(0, ...finite);
  if (low === high) {
    return { ticks: [0, 1], whole: true };
  }
  const exponent = Math.floor(Math.log10((high - low) / maxSteps));
  const candidates = [exponent, exponent + 1].flatMap((power) => NICE_STEPS.map((nice) => nice * 10 ** power));
  const stepsOf = (step: number): number => Math.ceil(high / step) - Math.floor(low / step);
  const step = candidates.find((candidate) => stepsOf(candidate) <= maxSteps) ?? 10 ** (exponent + 2);
  const first = Math.floor(low / step);
  // Multiply rather than add up, and trim float noise (0.1 + 0.2).
  const ticks = Array.from({ length: stepsOf(step) + 1 }, (_, index) => Number(((first + index) * step).toFixed(6)));
  return { ticks, whole: ticks.every((tick) => Number.isInteger(tick)) };
}
