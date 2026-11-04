// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The recurring-transaction form as data. People enter positive amounts and
 * pick bill or income; the stored range is signed like the ledger (a bill is
 * negative), so `amount_min <= amount_max` holds either way.
 */
import { isIsoDate } from "../../lib/dates";
import { amountSign, compareAmounts, isDecimalString, negateAmount } from "../../lib/money";
import { labelOf } from "../investments/format";
import type { Cadence, Schedule, ScheduleCreate } from "./api";

export interface ScheduleForm {
  name: string;
  accountId: string;
  currency: string;
  direction: "bill" | "income";
  /** Lowest expected amount, positive. */
  low: string;
  /** Highest expected amount, positive; blank means the same as `low`. */
  high: string;
  cadence: Cadence;
  intervalCount: string;
  nextDue: string;
  payeeId: string;
  categoryId: string;
}

export function emptyScheduleForm(today: string): ScheduleForm {
  return {
    name: "",
    accountId: "",
    currency: "USD",
    direction: "bill",
    low: "",
    high: "",
    cadence: "monthly",
    intervalCount: "1",
    nextDue: today,
    payeeId: "",
    categoryId: "",
  };
}

export type ScheduleProblems = Partial<Record<"name" | "account" | "amount" | "interval" | "nextDue", string>>;
export type ScheduleResult = { ok: true; body: ScheduleCreate } | { ok: false; problems: ScheduleProblems };

export function buildScheduleBody(form: ScheduleForm): ScheduleResult {
  const problems: ScheduleProblems = {};
  if (form.name.trim() === "") {
    problems.name = "Enter a name.";
  }
  if (form.accountId === "") {
    problems.account = "Choose an account.";
  }
  const low = form.low.trim();
  const high = form.high.trim() === "" ? low : form.high.trim();
  if (!isDecimalString(low) || amountSign(low) <= 0 || !isDecimalString(high) || amountSign(high) <= 0) {
    problems.amount = "Enter an amount above zero.";
  } else if (compareAmounts(low, high) > 0) {
    problems.amount = "The lowest amount is above the highest.";
  }
  if (!/^\d{1,3}$/.test(form.intervalCount.trim()) || Number(form.intervalCount) < 1) {
    problems.interval = "Enter a whole number of 1 or more.";
  }
  if (!isIsoDate(form.nextDue)) {
    problems.nextDue = "Enter a valid date.";
  }
  if (Object.keys(problems).length > 0) {
    return { ok: false, problems };
  }
  const [min, max] = form.direction === "bill" ? [negateAmount(high), negateAmount(low)] : [low, high];
  return {
    ok: true,
    body: {
      account_id: form.accountId,
      name: form.name.trim(),
      cadence: form.cadence,
      interval_count: Number(form.intervalCount),
      active: true,
      next_due: form.nextDue,
      amount_min: { amount: min, currency: form.currency },
      amount_max: { amount: max, currency: form.currency },
      ...(form.payeeId === "" ? {} : { payee_id: form.payeeId }),
      ...(form.categoryId === "" ? {} : { category_id: form.categoryId }),
    },
  };
}

/** "Every month", "Every 2 weeks". */
export function cadenceText(cadence: Cadence, count: number): string {
  const unit = labelOf({ daily: "day", weekly: "week", monthly: "month", quarterly: "quarter", yearly: "year" }, cadence);
  return count === 1 ? `Every ${unit}` : `Every ${String(count)} ${unit}s`;
}

export function isBill(schedule: Pick<Schedule, "amount_min" | "amount_max">): boolean {
  return amountSign(schedule.amount_max.amount) < 0 || (amountSign(schedule.amount_max.amount) === 0 && amountSign(schedule.amount_min.amount) < 0);
}
