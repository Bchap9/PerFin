// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The extra-payment simulator form as data: validation and the request body.
 * Amounts stay decimal strings; extras are principal in the loan currency.
 */
import { isIsoDate } from "../../lib/dates";
import { amountSign, isDecimalString } from "../../lib/money";
import type { SimulateIn } from "./api";

export interface OneOffRow {
  /** Row key for React; never sent. */
  key: string;
  on: string;
  amount: string;
}

export interface SimulatorForm {
  oneOff: OneOffRow[];
  recurringAmount: string;
  recurringStart: string;
  recurringEnd: string;
  includeSchedule: boolean;
}

export const MAX_ONE_OFF = 240;

export function emptySimulatorForm(): SimulatorForm {
  return { oneOff: [], recurringAmount: "", recurringStart: "", recurringEnd: "", includeSchedule: false };
}

export interface SimulatorProblems {
  form?: string;
  recurringAmount?: string;
  recurringStart?: string;
  recurringEnd?: string;
  /** Problem text by one-off row key. */
  rows: Record<string, string>;
}

export type SimulatorResult = { ok: true; body: SimulateIn } | { ok: false; problems: SimulatorProblems };

export function buildSimulateBody(form: SimulatorForm, currency: string): SimulatorResult {
  const problems: SimulatorProblems = { rows: {} };
  const oneOff: NonNullable<SimulateIn["one_off"]> = [];
  for (const row of form.oneOff) {
    const amount = row.amount.trim();
    if (!isIsoDate(row.on)) {
      problems.rows[row.key] = "Enter a valid date.";
    } else if (!isDecimalString(amount) || amountSign(amount) <= 0) {
      problems.rows[row.key] = "Enter an extra amount above zero.";
    } else {
      oneOff.push({ on: row.on, amount: { amount, currency } });
    }
  }
  if (form.oneOff.length > MAX_ONE_OFF) {
    problems.form = `At most ${String(MAX_ONE_OFF)} one-off payments.`;
  }
  const recurringAmount = form.recurringAmount.trim();
  let recurring: SimulateIn["recurring"] = null;
  if (recurringAmount !== "") {
    if (!isDecimalString(recurringAmount) || amountSign(recurringAmount) <= 0) {
      problems.recurringAmount = "Enter an extra amount above zero.";
    } else {
      const start = form.recurringStart.trim();
      const end = form.recurringEnd.trim();
      if (start !== "" && !isIsoDate(start)) {
        problems.recurringStart = "Enter a valid start date.";
      }
      if (end !== "" && !isIsoDate(end)) {
        problems.recurringEnd = "Enter a valid end date.";
      }
      if (start !== "" && end !== "" && isIsoDate(start) && isIsoDate(end) && end < start) {
        problems.recurringEnd = "The end date is before the start date.";
      }
      recurring = {
        amount: { amount: recurringAmount, currency },
        ...(start === "" ? {} : { start }),
        ...(end === "" ? {} : { end }),
      };
    }
  }
  if (oneOff.length === 0 && recurring === null && Object.keys(problems.rows).length === 0 && problems.recurringAmount === undefined) {
    problems.form = "Add a one-off payment or a recurring extra amount.";
  }
  const failed =
    problems.form !== undefined ||
    problems.recurringAmount !== undefined ||
    problems.recurringStart !== undefined ||
    problems.recurringEnd !== undefined ||
    Object.keys(problems.rows).length > 0;
  if (failed) {
    return { ok: false, problems };
  }
  return { ok: true, body: { one_off: oneOff, recurring, include_schedule: form.includeSchedule } };
}
