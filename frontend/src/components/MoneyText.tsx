// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Renders a money amount from its decimal string without converting it to a
 * float (lib/money.ts). The machine-readable amount is kept in a `<data>`
 * element's `value`. Negative amounts get a class for colour, and the minus
 * sign (or accounting parentheses) carries the meaning on its own.
 */
import { amountSign, formatMoney, type FormatMoneyOptions } from "../lib/money";
import { cx } from "./cx";

export interface MoneyTextProps extends FormatMoneyOptions {
  amount: string;
  currency: string;
  /** Colour negative/positive amounts (default: negatives only). */
  tone?: "auto" | "signed" | "none";
  className?: string;
}

export function MoneyText({ amount, currency, tone = "auto", className, ...format }: MoneyTextProps) {
  const text = formatMoney(amount, currency, format);
  if (text === null) {
    return (
      <span className={cx("money", "money--invalid", className)}>
        <span aria-hidden="true">—</span>
        <span className="visually-hidden">Amount unavailable</span>
      </span>
    );
  }
  const sign = amountSign(amount);
  const toneClass =
    tone === "none"
      ? null
      : sign < 0
        ? "money--negative"
        : tone === "signed" && sign > 0
          ? "money--positive"
          : null;
  return (
    <data value={amount} className={cx("money", toneClass, className)}>
      {text}
    </data>
  );
}
