// SPDX-License-Identifier: AGPL-3.0-or-later
/** Pure helpers for the budget report: status and bar widths, computed without floats until the last step. */
import { amountSign, compareAmounts, parseDecimal, roundDecimal } from "../../lib/money";

export type LineStatus = "ok" | "forecast-over" | "over";

export interface ReportAmounts {
  actual: { amount: string };
  available: { amount: string };
  forecast: { amount: string };
  remaining: { amount: string };
}

/**
 * `over` once spending passed what is available (remaining below zero),
 * `forecast-over` when the period is on course to pass it, otherwise `ok`.
 * Income lines are the opposite way round; callers pass `kind` so an income
 * line that has not reached its target is not flagged as overspent.
 */
export function lineStatus(line: ReportAmounts, kind: "expense" | "income" | "transfer" | "ignore" | null = "expense"): LineStatus {
  if (kind === "income") {
    return "ok";
  }
  if (amountSign(line.remaining.amount) < 0) {
    return "over";
  }
  if (compareAmounts(line.forecast.amount, line.available.amount) > 0) {
    return "forecast-over";
  }
  return "ok";
}

/** `part / whole` as a percentage with one decimal; `null` when `whole` is zero. */
export function percentOf(part: string, whole: string): number | null {
  const a = roundDecimal(parseDecimal(part), 6);
  const b = roundDecimal(parseDecimal(whole), 6);
  if (b.units === 0n) {
    return null;
  }
  return Number((a.units * 1000n) / b.units) / 10;
}

/** Width of a progress bar: 0 to 100 whatever the ratio. */
export function barWidth(part: string, whole: string): number {
  const percent = percentOf(part, whole);
  if (percent === null) {
    return amountSign(part) > 0 ? 100 : 0;
  }
  return Math.max(0, Math.min(100, percent));
}

/** The period to ask for when stepping `direction` periods from `start`. */
export function steppedPeriod(start: string, period: "monthly" | "annual" | "custom", direction: -1 | 1, addMonths: (iso: string, n: number) => string): string | null {
  if (period === "custom") {
    return null;
  }
  return addMonths(start, direction * (period === "annual" ? 12 : 1));
}
