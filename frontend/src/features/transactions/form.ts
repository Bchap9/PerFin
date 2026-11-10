// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The add/edit transaction form without the UI: a positive amount plus a
 * "money out / money in" choice instead of a signed number, and the request
 * bodies built from the form (exact decimal strings, lib/money.ts; no floats).
 */
import { amountSign, compareAmounts, currencyFractionDigits, decimalToString, negateAmount, tryParseDecimal } from "../../lib/money";
import type { Transaction, TransactionCreate, TransactionStatus, TransactionUpdate } from "./api";
import { normalizeAmountInput } from "./splits";

/** `out` is money leaving the account (stored as a negative amount), `in` money arriving. */
export type Direction = "out" | "in";

export interface TransactionForm {
  accountId: string;
  /** `YYYY-MM-DD`. */
  date: string;
  direction: Direction;
  /** What the person typed: a positive amount such as "12.50". */
  amount: string;
  payeeId: string;
  description: string;
  categoryId: string;
  memo: string;
  status: TransactionStatus;
}

export type AmountResult = { ok: true; amount: string } | { ok: false; error: string };

/** Check a typed amount: a plain positive number with no more decimals than the currency has. */
export function parsePositiveAmount(input: string, currency: string): AmountResult {
  const trimmed = input.trim();
  const normalized = normalizeAmountInput(trimmed.startsWith(".") ? `0${trimmed}` : trimmed);
  const value = normalized === null ? null : tryParseDecimal(normalized);
  if (normalized === null || value === null) {
    return { ok: false, error: "Enter an amount such as 12.50." };
  }
  if (normalized.startsWith("-") || normalized.startsWith("+")) {
    return { ok: false, error: "Enter the amount without a sign, then choose money out or money in." };
  }
  if (value.units === 0n) {
    return { ok: false, error: "Enter an amount above zero." };
  }
  const digits = currencyFractionDigits(currency);
  if (value.scale > digits) {
    return {
      ok: false,
      error: digits === 0 ? `${currency} amounts have no decimal places.` : `Use at most ${String(digits)} decimal places for ${currency}.`,
    };
  }
  return { ok: true, amount: decimalToString(value) };
}

/** The signed amount the API stores: negative for money out. */
export function signedAmount(direction: Direction, positive: string): string {
  return direction === "out" ? negateAmount(positive) : positive;
}

/** A stored signed amount as the form shows it ("-42.10" is 42.10 out). */
export function unsignedAmount(amount: string): { direction: Direction; amount: string } {
  if (tryParseDecimal(amount) === null) {
    return { direction: "out", amount: "" };
  }
  return amountSign(amount) < 0 ? { direction: "out", amount: negateAmount(amount) } : { direction: "in", amount };
}

function textOrNull(value: string): string | null {
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

/** The body of `POST /transactions`; `amount` is the checked positive amount. */
export function createBody(form: TransactionForm, amount: string, currency: string): TransactionCreate {
  const description = textOrNull(form.description);
  const memo = textOrNull(form.memo);
  return {
    account_id: form.accountId,
    posted_date: form.date,
    amount: { amount: signedAmount(form.direction, amount), currency },
    status: form.status,
    ...(form.payeeId === "" ? {} : { payee_id: form.payeeId }),
    ...(description === null ? {} : { raw_payee: description }),
    ...(form.categoryId === "" ? {} : { category_id: form.categoryId }),
    ...(memo === null ? {} : { memo }),
  };
}

/**
 * The body of `PATCH /transactions/{id}`: only what the person changed
 * (an absent field is left alone by the server, `null` clears it).
 * `amount` is the checked positive amount, or `null` to leave the amount alone.
 */
export function updateBody(row: Transaction, form: TransactionForm, amount: string | null): TransactionUpdate {
  const patch: TransactionUpdate = {};
  if (form.date !== row.posted_date) {
    patch.posted_date = form.date;
  }
  if (amount !== null) {
    const signed = signedAmount(form.direction, amount);
    if (tryParseDecimal(row.amount.amount) === null || compareAmounts(signed, row.amount.amount) !== 0) {
      patch.amount = { amount: signed, currency: row.amount.currency };
    }
  }
  const payeeId = form.payeeId === "" ? null : form.payeeId;
  if (payeeId !== row.payee_id) {
    patch.payee_id = payeeId;
  }
  const description = textOrNull(form.description);
  if (description !== textOrNull(row.raw_payee ?? "")) {
    patch.raw_payee = description;
  }
  const categoryId = form.categoryId === "" ? null : form.categoryId;
  if (row.splits.length === 0 && categoryId !== row.category_id) {
    patch.category_id = categoryId;
  }
  const memo = textOrNull(form.memo);
  if (memo !== textOrNull(row.memo ?? "")) {
    patch.memo = memo;
  }
  if (form.status !== row.status) {
    patch.status = form.status;
  }
  return patch;
}

/** Why the server will not change this transaction's amount (it answers 409), or `null` when it may change. */
export function amountLock(row: Transaction): string | null {
  if (row.splits.length > 0) {
    return "This transaction is split. Change or remove the split to change the amount.";
  }
  if (row.transfer !== null) {
    return "This is one leg of a transfer. Unlink the transfer to change the amount.";
  }
  return null;
}
