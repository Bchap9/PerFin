// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Report periods: the presets of the control bar, a custom range and the
 * checks the server would make (backend `app.domain.reports.dates`:
 * `date_from <= date_to`, at most 3660 days). Pure functions on `YYYY-MM-DD`
 * strings; "today" is always passed in.
 */
import { addMonths, daysBetween, endOfMonth, formatDate, isIsoDate, parseIsoDate, startOfMonth } from "../../lib/dates";

export const PERIOD_IDS = ["this-month", "last-month", "last-3-months", "year-to-date", "last-year", "last-12-months", "custom"] as const;

export type PeriodId = (typeof PERIOD_IDS)[number];
export type PresetId = Exclude<PeriodId, "custom">;

export const PERIOD_OPTIONS: readonly { value: PeriodId; label: string }[] = [
  { value: "this-month", label: "This month" },
  { value: "last-month", label: "Last month" },
  { value: "last-3-months", label: "Last 3 months" },
  { value: "year-to-date", label: "Year to date" },
  { value: "last-year", label: "Last year" },
  { value: "last-12-months", label: "Last 12 months" },
  { value: "custom", label: "Custom dates" },
];

export function isPeriodId(value: unknown): value is PeriodId {
  return typeof value === "string" && PERIOD_IDS.some((id) => id === value);
}

export interface DateRange {
  from: string;
  to: string;
}

/** Longest range the server accepts, in days, both ends included. */
export const MAX_REPORT_DAYS = 3660;

/**
 * The dates of a preset. Every preset covers whole calendar months, and one
 * that includes the current month runs to the end of it: that is the range
 * the dashboard and the server's own defaults use ("this month and the
 * eleven before"), so the same month shows the same figure everywhere, also
 * when something is already dated later this month.
 */
export function presetRange(id: PresetId, today: string): DateRange {
  const monthStart = startOfMonth(today);
  const monthEnd = endOfMonth(today);
  const year = parseIsoDate(today).year;
  const pad = (value: number): string => String(value).padStart(4, "0");
  switch (id) {
    case "this-month":
      return { from: monthStart, to: monthEnd };
    case "last-month": {
      const start = addMonths(monthStart, -1);
      return { from: start, to: endOfMonth(start) };
    }
    case "last-3-months":
      return { from: addMonths(monthStart, -2), to: monthEnd };
    case "year-to-date":
      return { from: `${pad(year)}-01-01`, to: monthEnd };
    case "last-year":
      return { from: `${pad(year - 1)}-01-01`, to: `${pad(year - 1)}-12-31` };
    case "last-12-months":
      return { from: addMonths(monthStart, -11), to: monthEnd };
  }
}

export type RangeProblem = "missing" | "from-after-to" | "too-long";

/** What is wrong with a custom range, or `null` when the server would accept it. */
export function rangeProblem(from: string, to: string): RangeProblem | null {
  if (!isIsoDate(from) || !isIsoDate(to)) {
    return "missing";
  }
  if (from > to) {
    return "from-after-to";
  }
  return daysBetween(from, to) + 1 > MAX_REPORT_DAYS ? "too-long" : null;
}

export function rangeProblemText(problem: RangeProblem): string {
  switch (problem) {
    case "missing":
      return "Choose a start date and an end date.";
    case "from-after-to":
      return "The start date is after the end date.";
    case "too-long":
      return "A report covers at most ten years. Choose a shorter period.";
  }
}

export interface ResolvedPeriod {
  /** The dates to ask for; `null` while a custom range is incomplete or invalid. */
  range: DateRange | null;
  problem: RangeProblem | null;
}

export function resolvePeriod(id: PeriodId, custom: DateRange, today: string): ResolvedPeriod {
  if (id !== "custom") {
    return { range: presetRange(id, today), problem: null };
  }
  const problem = rangeProblem(custom.from, custom.to);
  return problem === null ? { range: { from: custom.from, to: custom.to }, problem: null } : { range: null, problem };
}

/** `YYYY-MM` of a date. */
export function monthOf(iso: string): string {
  return iso.slice(0, 7);
}

/** `YYYY-MM` moved by whole months. */
export function shiftMonth(month: string, months: number): string {
  return monthOf(addMonths(`${month}-01`, months));
}

/** "Nov 1, 2026 to Nov 30, 2026" in the viewer's locale. */
export function describeRange(range: DateRange, locale?: string): string {
  const options = locale === undefined ? {} : { locale };
  return `${formatDate(range.from, options) ?? range.from} to ${formatDate(range.to, options) ?? range.to}`;
}
