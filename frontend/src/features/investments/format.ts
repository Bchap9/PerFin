// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Decimal-string helpers for the wealth screens: fractions as percentages,
 * gain ratios and bar widths, all with integer (bigint) arithmetic. Nothing
 * here converts an amount to a JavaScript number.
 */
import {
  amountSign,
  decimalToString,
  formatDecimal,
  parseDecimal,
  roundDecimal,
  tryParseDecimal,
  type Decimal,
} from "../../lib/money";

/** A fraction ("0.0825") as percent units ("8.25"), rounded half away from zero. */
export function fractionToPercentAmount(fraction: string, digits = 2): string | null {
  const value = tryParseDecimal(fraction);
  if (value === null) {
    return null;
  }
  const shifted: Decimal =
    value.scale >= 2
      ? { units: value.units, scale: value.scale - 2 }
      : { units: value.units * 10n ** BigInt(2 - value.scale), scale: 0 };
  return decimalToString(roundDecimal(shifted, digits));
}

/** "8.25%" from a fraction; "n/a" for a missing or malformed one. */
export function formatFraction(fraction: string | null | undefined, digits = 2): string {
  if (fraction === null || fraction === undefined) {
    return "n/a";
  }
  const percent = fractionToPercentAmount(fraction, digits);
  return percent === null ? "n/a" : `${formatDecimal(percent, { minimumFractionDigits: digits }) ?? percent}%`;
}

/** `numerator / denominator` as a percent amount ("12.50"); null when the denominator is zero. */
export function ratioPercent(numerator: string, denominator: string, digits = 2): string | null {
  const n = tryParseDecimal(numerator);
  const d = tryParseDecimal(denominator);
  if (n === null || d === null || d.units === 0n) {
    return null;
  }
  // (n.units / 10^n.scale) / (d.units / 10^d.scale) * 100, scaled to `digits + 1` digits then rounded.
  const extra = BigInt(digits + 1);
  const numeratorScaled = n.units * 100n * 10n ** (extra + BigInt(d.scale));
  const denominatorScaled = d.units * 10n ** BigInt(n.scale);
  const quotient = numeratorScaled / denominatorScaled; // truncates toward zero
  return decimalToString(roundDecimal({ units: quotient, scale: digits + 1 }, digits));
}

export type GainTone = "gain" | "loss" | "flat";

export interface GainDisplay {
  /** The gain as given, e.g. "125.50" or "-40.00". */
  amount: string;
  /** Gain as a percent of cost, e.g. "12.55"; null when the cost is zero. */
  percent: string | null;
  tone: GainTone;
  /** "+12.55%" / "-3.20%" / "0.00%"; "n/a" when the cost is zero. */
  percentLabel: string;
}

/** How a gain reads next to the cost it was made on. */
export function gainDisplay(gain: string, cost: string): GainDisplay {
  const sign = amountSign(gain);
  const percent = ratioPercent(gain, cost);
  const tone: GainTone = sign > 0 ? "gain" : sign < 0 ? "loss" : "flat";
  let percentLabel = "n/a";
  if (percent !== null) {
    const shown = formatDecimal(percent, { minimumFractionDigits: 2, signDisplay: "exceptZero" }) ?? percent;
    percentLabel = `${shown}%`;
  }
  return { amount: gain, percent, tone, percentLabel };
}

/** A weight fraction ("0.2534") as a CSS width ("25.34%"), clamped to 0..100. */
export function weightWidth(weight: string): string {
  const percent = fractionToPercentAmount(weight, 2);
  if (percent === null) {
    return "0%";
  }
  const value = parseDecimal(percent);
  if (value.units <= 0n) {
    return "0%";
  }
  if (value.units > 100n * 10n ** BigInt(value.scale)) {
    return "100%";
  }
  return `${percent}%`;
}

/** Quantities and unit prices: grouped, no trailing zeros beyond what the API sent. */
export function formatQuantity(quantity: string): string {
  return formatDecimal(quantity) ?? quantity;
}

/** Exact product of two decimal strings, rounded to `digits` places; null if either is malformed. */
export function multiplyAmounts(a: string, b: string, digits: number): string | null {
  const x = tryParseDecimal(a);
  const y = tryParseDecimal(b);
  if (x === null || y === null) {
    return null;
  }
  return decimalToString(roundDecimal({ units: x.units * y.units, scale: x.scale + y.scale }, digits));
}

/** Percent units typed by a person ("6.125") as the API fraction ("0.06125"); null if malformed. */
export function percentToFraction(percent: string): string | null {
  const value = tryParseDecimal(percent.trim());
  if (value === null) {
    return null;
  }
  return decimalToString({ units: value.units, scale: value.scale + 2 });
}

/** Look a label up in a table keyed by a string union without an indexed access. */
export function labelOf<K extends string>(table: Readonly<Record<K, string>>, key: K): string {
  return new Map<string, string>(Object.entries(table)).get(key) ?? key;
}

/** The parts of a security its labels are made of. */
export interface SecurityNames {
  symbol: string | null;
  name: string;
}

/**
 * A security's short label: its symbol, else its name (a security an imported
 * statement printed by its name alone has no symbol).
 */
export function securityLabel(security: SecurityNames): string {
  return security.symbol ?? security.name;
}

/** "SYM - Name" for a picker; the name alone for a security without a symbol. */
export function securityOptionLabel(security: SecurityNames): string {
  return security.symbol === null ? security.name : `${security.symbol} - ${security.name}`;
}
