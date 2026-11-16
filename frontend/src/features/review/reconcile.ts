// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The arithmetic and the words of the reconcile view, without React.
 *
 * Balances are typed as printed (`1,204.18`, `$12.00`, `(3.10)`); the server
 * reads them the same way and stays the authority. Sums use the exact
 * decimals of lib/money.ts (scaled integers): nothing here is a float.
 */
import {
  addDecimals,
  compareDecimals,
  decimalToString,
  formatMoney,
  negateDecimal,
  roundDecimal,
  tryParseDecimal,
  type Decimal,
} from "../../lib/money";
import type { CauseKind, ReconcileCause, StatementReconciliation } from "./api";

export type PrintedSign = StatementReconciliation["printed_sign"];
export type LiveState = "balanced" | "off" | "unchecked" | "invalid";

export interface LiveDifference {
  /** `opening + rows - closing` in ledger terms, as decimal text ("" when unknown). */
  text: string;
  state: LiveState;
}

/** Longest balance the server accepts (`PrintedAmount`). */
const PRINTED_MAX = 32;
const CURRENCY_SYMBOLS: ReadonlySet<string> = new Set(["$", "€", "£"]);
/** The server's rounding tolerance: half a cent. */
const TOLERANCE: Decimal = { units: 5n, scale: 3 };
const DIGITS = /^\d+$/;

function wholeDigits(text: string): string | null {
  if (!text.includes(",")) {
    return DIGITS.test(text) ? text : null;
  }
  const [head = "", ...groups] = text.split(",");
  if (!DIGITS.test(head) || head.length > 3) {
    return null;
  }
  if (!groups.every((group) => group.length === 3 && DIGITS.test(group))) {
    return null;
  }
  return head + groups.join("");
}

/**
 * A balance as a person reads it off the page, as plain decimal text:
 * spaces, thousands separators and one currency symbol are dropped; a
 * leading minus or surrounding brackets make it negative. `null` for
 * anything else.
 */
export function readPrinted(text: string): string | null {
  let rest = text.replace(/\s+/g, "");
  if (rest === "" || rest.length > PRINTED_MAX) {
    return null;
  }
  let negative = false;
  if (rest.startsWith("(") && rest.endsWith(")")) {
    negative = true;
    rest = rest.slice(1, -1);
  }
  const minus = (): boolean => {
    if (!rest.startsWith("-")) {
      return true;
    }
    if (negative) {
      return false;
    }
    negative = true;
    rest = rest.slice(1);
    return true;
  };
  if (!minus()) {
    return null;
  }
  if (CURRENCY_SYMBOLS.has(rest.charAt(0))) {
    rest = rest.slice(1);
    if (!minus()) {
      return null;
    }
  } else if (CURRENCY_SYMBOLS.has(rest.charAt(rest.length - 1))) {
    rest = rest.slice(0, -1);
  }
  const parts = rest.split(".");
  if (parts.length > 2) {
    return null;
  }
  const whole = wholeDigits(parts[0] ?? "");
  const fraction = parts[1];
  if (whole === null || (fraction !== undefined && !DIGITS.test(fraction))) {
    return null;
  }
  const plain = fraction === undefined ? whole : `${whole}.${fraction}`;
  if (tryParseDecimal(plain) === null) {
    return null;
  }
  return negative ? `-${plain}` : plain;
}

function printedDecimal(text: string): Decimal | null {
  const plain = readPrinted(text);
  return plain === null ? null : tryParseDecimal(plain);
}

function toLedger(value: Decimal, printedSign: PrintedSign): Decimal {
  return printedSign === "owed" ? negateDecimal(value) : value;
}

function absolute(value: Decimal): Decimal {
  return value.units < 0n ? negateDecimal(value) : value;
}

/**
 * The difference for the balances as typed: `opening + total - closing` in
 * ledger terms (a balance owed is negated first). `unchecked` until both
 * balances are typed, `invalid` when one is not an amount, `balanced` within
 * half a cent.
 */
export function liveDifference(
  opening: string,
  closing: string,
  total: string,
  printedSign: PrintedSign,
): LiveDifference {
  if (opening.trim() === "" || closing.trim() === "") {
    return { text: "", state: "unchecked" };
  }
  const first = printedDecimal(opening);
  const last = printedDecimal(closing);
  const rows = tryParseDecimal(total);
  if (first === null || last === null || rows === null) {
    return { text: "", state: "invalid" };
  }
  const difference = addDecimals(
    addDecimals(toLedger(first, printedSign), rows),
    negateDecimal(toLedger(last, printedSign)),
  );
  const balanced = compareDecimals(absolute(difference), TOLERANCE) <= 0;
  return {
    text: decimalToString(balanced ? roundDecimal(difference, 2, "trunc") : difference),
    state: balanced ? "balanced" : "off",
  };
}

/**
 * What the closing balance should be for the opening balance as typed and
 * the rows in the ledger, in the terms the statement prints.
 */
export function expectedClosing(
  opening: string,
  total: string,
  printedSign: PrintedSign,
): string | null {
  const first = printedDecimal(opening);
  const rows = tryParseDecimal(total);
  if (first === null || rows === null) {
    return null;
  }
  const ledger = addDecimals(toLedger(first, printedSign), rows);
  return decimalToString(toLedger(ledger, printedSign));
}

function money(amount: string, currency: string): string {
  return formatMoney(amount, currency) ?? `${amount} ${currency}`;
}

function magnitude(amount: string): string {
  return amount.startsWith("-") ? amount.slice(1) : amount;
}

/** The sentence under the difference. */
export function statusText(
  state: LiveState,
  difference: string | null,
  currency: string,
): string {
  if (state === "balanced") {
    return "Balanced.";
  }
  if (state === "invalid") {
    return "Enter each balance as a number, the way the statement prints it.";
  }
  if (state === "off" && difference !== null && difference !== "") {
    return `Off by ${money(magnitude(difference), currency)}.`;
  }
  return "No balances to check against. Enter them as printed on the statement.";
}

/** The status of a statement as the server computed it. */
export function statusLine(statement: StatementReconciliation): string {
  return statusText(statement.status, statement.difference, statement.currency);
}

type CauseWriter = (amount: string | null) => string;

const CAUSES: ReadonlyMap<CauseKind, CauseWriter> = new Map<CauseKind, CauseWriter>([
  [
    "extra_row",
    () =>
      "One row is exactly the difference. It may be a row too many, such as a total or a balance line read as a transaction.",
  ],
  [
    "sign_flipped",
    () => "The difference is twice this row. Its sign may be the wrong way round.",
  ],
  [
    "transposed_digits",
    () => "The difference divides by 9. Two digits of an amount may be swapped.",
  ],
  [
    "fee_or_interest",
    () =>
      "A fee or interest line on the statement has the size of the difference and was not read as a row.",
  ],
  [
    "opening_differs_from_previous",
    (amount) =>
      amount === null
        ? "The opening balance is not the closing balance of the previous statement."
        : `The opening balance is not the closing balance of the previous statement (${amount}).`,
  ],
  [
    "duplicated_row",
    () => "These rows have the same date and amount. One may be a duplicate.",
  ],
  ["out_of_period", () => "These rows are dated outside the statement period."],
  [
    "earlier_reading",
    () =>
      "These rows come from an earlier reading of this file. They were kept because they had been changed.",
  ],
  [
    "missing_row",
    (amount) =>
      amount === null
        ? "No row explains the difference. A row may be missing."
        : `No row explains the difference. A row of ${amount} may be missing.`,
  ],
]);

/** Every kind of cause the server ranks, in the order of the API. */
export const CAUSE_KINDS: readonly CauseKind[] = [...CAUSES.keys()];

/** One likely cause of the difference, in a sentence. */
export function causeText(cause: ReconcileCause, currency: string): string {
  const write = CAUSES.get(cause.kind);
  const amount = cause.amount === null ? null : money(cause.amount, currency);
  return write === undefined
    ? "The rows and the balances of the statement do not agree."
    : write(amount);
}

/** How many corrections of one kind suggest the layout itself is read wrongly. */
const TEACH_AFTER = 3;

/** After several of the same correction: teach the layout instead. */
export function teachHint(edits: { flipped: number; removed: number }): string | null {
  return edits.flipped >= TEACH_AFTER || edits.removed >= TEACH_AFTER
    ? "PerFin read several rows of this layout wrongly. Teach it the layout so the next statement reads right."
    : null;
}

/** The lines of the statement the reader passed over. */
export function skippedText(count: number): string | null {
  if (count <= 0) {
    return null;
  }
  return count === 1
    ? "1 line of the statement was not read. It is marked on the page."
    : `${String(count)} lines of the statement were not read. They are marked on the page.`;
}
