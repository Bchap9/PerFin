// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Split validation with exact decimals (lib/money.ts): no floats. The lines
 * of a split must be at least two and their amounts must add up exactly to
 * the transaction's amount (the server checks again, `422 split-sum-mismatch`).
 */
import { amountSign, isDecimalString, subtractAmounts, sumAmounts } from "../../lib/money";

export interface SplitDraft {
  /** Local key for React lists. */
  key: string;
  amount: string;
  categoryId: string;
  memo: string;
}

export interface SplitValidation {
  ok: boolean;
  /** Transaction amount minus the sum of the valid lines ("0" when balanced). */
  remainder: string | null;
  /** One message per line index with a problem. */
  lineErrors: ReadonlyMap<number, string>;
  /** A problem with the split as a whole. */
  formError: string | null;
}

/** Normalise user input ("1,234.50", " 12 ") to a plain decimal string, or null. */
export function normalizeAmountInput(input: string): string | null {
  const trimmed = input.trim().replace(/,/g, "");
  return isDecimalString(trimmed) ? trimmed : null;
}

export function validateSplits(total: string, lines: readonly SplitDraft[]): SplitValidation {
  const lineErrors = new Map<number, string>();
  const amounts: string[] = [];
  lines.forEach((line, index) => {
    const amount = normalizeAmountInput(line.amount);
    if (amount === null) {
      lineErrors.set(index, "Enter an amount such as 12.50.");
    } else if (amountSign(amount) === 0) {
      lineErrors.set(index, "An amount cannot be zero.");
    } else {
      amounts.push(amount);
    }
  });
  const hasLineErrors = lineErrors.size > 0;
  const remainder = hasLineErrors ? null : subtractAmounts(total, sumAmounts(amounts));
  let formError: string | null = null;
  if (lines.length === 1) {
    formError = "A split needs at least two lines.";
  } else if (remainder !== null && amountSign(remainder) !== 0) {
    formError = `The lines are off by ${remainder}.`;
  }
  return {
    ok: lines.length >= 2 && !hasLineErrors && remainder !== null && amountSign(remainder) === 0,
    remainder,
    lineErrors,
    formError,
  };
}

/** What is left to allocate: the total minus the lines with a valid amount ("0" when balanced). */
export function suggestedRemainder(total: string, lines: readonly SplitDraft[]): string {
  const valid = lines.map((line) => normalizeAmountInput(line.amount)).filter((amount): amount is string => amount !== null);
  return subtractAmounts(total, sumAmounts(valid));
}
